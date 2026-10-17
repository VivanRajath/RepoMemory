"""Graph-first retrieval: seeds -> bounded Cypher expansion -> deterministic scoring -> token-budgeted package."""
import math
from datetime import datetime, timezone

import tiktoken
from neo4j.exceptions import ServiceUnavailable, SessionExpired

from graph import run, session, uid
from ingest.code_ingest import component_key, load_roots
from retrieve.parse_query import parse

ENC = tiktoken.get_encoding("cl100k_base")
HALF_LIFE_DAYS = 90
SECTIONS = ["question", "components", "code", "decisions", "bugs", "failed_attempts", "history",
            "dependency_changes", "coupled_files"]

# Expansion patterns from a Component `c`. The last node of each path is the candidate.
# Each is 1-3 hops; component distance (0 for seeds, 1 for neighbours) is added on top.
PATTERNS = [
    "(c)<-[:AFFECTS]-(:Decision)",
    "(c)<-[:AFFECTS]-(:Decision)-[:IMPLEMENTED_BY]->(:Commit)",
    "(c)<-[:AFFECTS]-(:Decision)-[:SUPERSEDES]-(:Decision)",
    "(c)<-[:OCCURRED_IN]-(:Bug)",
    "(c)<-[:OCCURRED_IN]-(:Bug)<-[:RESOLVES]-(:Fix)",
    "(c)<-[:OCCURRED_IN]-(:Bug)<-[:RESOLVES]-(:Fix)-[:IMPLEMENTED_BY]->(:Commit)",
    "(c)<-[:OCCURRED_IN]-(:Bug)<-[:RESOLVES]-(:Fix)<-[:VALIDATES]-(:TestRun)",
    "(c)<-[:OCCURRED_IN]-(:Bug)<-[:ATTEMPTED_FOR]-(:FailedAttempt)",
    "(c)<-[:OCCURRED_IN]-(:Bug)-[:SIMILAR_TO]-(:Bug)",
    "(c)-[:CONTAINS]->(:File)<-[:MODIFIED]-(:Commit)<-[:IMPLEMENTED_BY]-(:FailedAttempt)",
    "(c)-[:USES]->(:Dependency)",
    "(c)-[:USES]->(:Dependency)<-[:BUMPED]-(:Commit)",
]


def count_tokens(text: str) -> int:
    return len(ENC.encode(text))


def _node(n) -> dict:
    props = {k: v for k, v in dict(n).items() if k not in ("embedding", "ext_imports")}
    return {"label": next(iter(n.labels)), **props}


def _path(p) -> dict:
    return {"nodes": [n["uid"] for n in p.nodes],
            "rels": [[r.start_node["uid"], r.type, r.end_node["uid"]] for r in p.relationships]}


class Candidates:
    def __init__(self):
        self.items: dict[str, dict] = {}

    def add(self, node: dict, hop: int, path: dict | None, **extra):
        cur = self.items.get(node["uid"])
        if cur is None or hop < cur["hop"]:
            self.items[node["uid"]] = {"node": node, "hop": hop, "path": path, **extra}
        elif extra:
            cur.update({k: v for k, v in extra.items() if k not in cur})

    def by_label(self, label):
        return [c for c in self.items.values() if c["node"]["label"] == label]


def _paths(session, query, **params):
    return [(r["p"], r.get("d")) for r in session.run(query, **params)]


def find_seeds(project: str, q: dict, s) -> tuple[dict, dict, Candidates]:
    """Returns (seed_files {uid: why}, comp_dist {uid: distance}, candidates)."""
    cands = Candidates()
    seed_files: dict[str, str] = {}
    for f in q["frames"]:
        seed_files[uid("file", project, f["path"])] = f"stack frame {f['path']}:{f['line']}"
    for ep in q["endpoints"]:
        for r in s.run("""MATCH (f:File {project: $p})-[:DEFINES]->(s:Symbol {kind: 'route'})
                          WHERE s.name ENDS WITH $ep OR s.name CONTAINS $ep + '/' RETURN f.uid AS f, s""", p=project, ep=ep):
            seed_files[r["f"]] = f"defines route {r['s']['name']}"
            cands.add(_node(r["s"]), 0, None)
    seed_comps: dict[str, str] = {}
    if q["fulltext"]:
        for r in s.run("""CALL db.index.fulltext.queryNodes('entity_names', $q) YIELD node, score
                          WHERE node.project = $p RETURN node, score LIMIT 8""", q=q["fulltext"], p=project):
            n = _node(r["node"])
            if n["label"] == "Component":
                seed_comps[n["uid"]] = "keyword match"
            elif n["label"] == "File" and r["score"] > 1.0 and not n.get("deleted"):
                seed_files.setdefault(n["uid"], "keyword match")
            elif n["label"] == "Dependency":
                cands.add(n, 1, None, fulltext=r["score"])
        for r in s.run("""CALL db.index.fulltext.queryNodes('knowledge_text', $q) YIELD node, score
                          WHERE node.project = $p RETURN node, score LIMIT 8""", q=q["fulltext"], p=project):
            cands.add(_node(r["node"]), 1, {"nodes": [r["node"]["uid"]], "rels": []}, fulltext=r["score"])
    for d in q["packages"]:
        for r in s.run("MATCH (d:Dependency {uid: $u}) RETURN d", u=d["uid"]):
            cands.add(_node(r["d"]), 0, {"nodes": [d["uid"]], "rels": []})

    for p, _ in _paths(s, """MATCH p=(c:Component)-[:CONTAINS]->(f:File) WHERE f.uid IN $fs RETURN p""", fs=list(seed_files)):
        cands.add(_node(p.nodes[0]), 0, _path(p))
        seed_comps.setdefault(p.nodes[0]["uid"], "contains " + p.nodes[1]["path"])
    for p, _ in _paths(s, "MATCH p=(f:File) WHERE f.uid IN $fs RETURN p", fs=list(seed_files)):
        cands.add(_node(p.nodes[0]), 0, _path(p), seed=True)
    for r in s.run("MATCH (c:Component) WHERE c.uid IN $cs RETURN c", cs=list(seed_comps)):
        cands.add(_node(r["c"]), 0, {"nodes": [r["c"]["uid"]], "rels": []}, seed=True, why=seed_comps[r["c"]["uid"]])
    if not seed_comps and not seed_files:  # general question ("what's in this repo?"): overview of all components
        for r in s.run("MATCH (c:Component {project: $p}) RETURN c", p=project):
            seed_comps[r["c"]["uid"]] = "repo overview"
            cands.add(_node(r["c"]), 0, {"nodes": [r["c"]["uid"]], "rels": []}, seed=True, why="repo overview")
    comp_dist = {c: 0 for c in seed_comps}
    return seed_files, comp_dist, cands


def expand(project: str, q: dict, seed_files: dict, comp_dist: dict, cands: Candidates, s):
    seeds = list(comp_dist)
    # Neighbour components: DEPENDS_ON either way, and components of strongly co-changed files.
    for p, _ in _paths(s, """MATCH p=(c:Component)-[:DEPENDS_ON]-(n:Component) WHERE c.uid IN $cs RETURN p""", cs=seeds):
        n = p.nodes[-1]
        comp_dist.setdefault(n["uid"], 1)
        cands.add(_node(n), 1, _path(p))
    for p, _ in _paths(s, """MATCH p=(c:Component)-[:CONTAINS]->(f:File)-[r:CO_CHANGED]-(g:File)<-[:CONTAINS]-(c2:Component)
                             WHERE c.uid IN $cs AND r.count >= 3 RETURN p""", cs=seeds):
        rel = p.relationships[1]
        g, c2 = p.nodes[2], p.nodes[3]
        from_seed_file = p.nodes[1]["uid"] in seed_files
        cands.add(_node(g), 1 if from_seed_file else 2, _path(p), cochange=rel["count"],
                  partner=p.nodes[1]["path"])
        if c2["uid"] != p.nodes[0]["uid"]:
            comp_dist.setdefault(c2["uid"], 1)
            cands.add(_node(c2), 1, _path(p), cochange=rel["count"])

    for pat in PATTERNS:
        for p, _ in _paths(s, f"MATCH p={pat} WHERE c.uid IN $cs RETURN p", cs=list(comp_dist)):
            start = p.nodes[0]["uid"]
            hop = comp_dist[start] + len(p.relationships)
            if hop > 4:
                continue
            extra = {}
            if p.relationships and p.relationships[-1].type == "BUMPED":
                r = p.relationships[-1]
                extra["bump"] = {"dep": p.nodes[-2]["uid"], "from": r["from"], "to": r["to"]}
            cands.add(_node(p.nodes[-1]), hop, _path(p), **extra)
            if extra:  # keep every bump, not just the best path to the commit
                cands.items[p.nodes[-1]["uid"]].setdefault("bumps", []).append(extra["bump"])

    # Vector search on the error signature, if the question has one.
    sig = " ".join(q["exceptions"] + q["status_codes"] + q["endpoints"])
    if sig:
        from ingest.knowledge_ingest import embed
        vec = embed([f"{sig} {' '.join(q['keywords'][:6])}"])[0].tolist()
        for r in s.run("""CALL db.index.vector.queryNodes('bug_embedding', 5, $v) YIELD node, score
                          WHERE node.project = $p AND score > 0.55 RETURN node, score""", v=vec, p=project):
            cands.add(_node(r["node"]), 1, {"nodes": [r["node"]["uid"]], "rels": []}, vector=r["score"])

    # Anchor rule: earliest Decision per seed component, regardless of score.
    for r in s.run("""MATCH p=(c:Component)<-[:AFFECTS]-(d:Decision) WHERE c.uid IN $cs
                      WITH c, p, d ORDER BY d.ts ASC
                      WITH c, head(collect(p)) AS p RETURN p""", cs=seeds):
        p = r["p"]
        cands.add(_node(p.nodes[-1]), 1, _path(p))
        cands.items[p.nodes[-1]["uid"]]["anchor"] = True


def score(cands: Candidates, q: dict, now_ts: int):
    w = {"Decision": 2.0 if q["intent"]["decision"] else 1.0,
         "Bug": 2.0 if q["intent"]["bug"] else 1.0,
         "Fix": 2.0 if q["intent"]["bug"] else 1.0,
         "FailedAttempt": 2.0 if q["intent"]["bug"] else 1.0,
         "Dependency": 2.0 if q["intent"]["dependency"] else 1.0}
    for c in cands.items.values():
        n = c["node"]
        s = 1.0 / max(c["hop"], 1) * w.get(n["label"], 1.0)
        if n.get("ts"):
            age_days = max(0, now_ts - n["ts"]) / 86400
            s *= 0.5 ** (age_days / HALF_LIFE_DAYS)
        if c.get("cochange"):
            s *= min(1.5, 0.5 + c["cochange"] / 6)
        if c.get("fulltext"):
            s *= 1 + min(c["fulltext"], 3) / 3
        if c.get("vector"):
            s *= 1 + c["vector"]
        if c.get("bumps"):
            s *= 2.0 if q["intent"]["dependency"] else 1.2
        c["score"] = s


# ---------- package compilation ----------

def _date(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d") if ts else "?"


def _cite(u):
    return f"[node:{u}]"


def build_sections(project: str, q: dict, question: str, stack_trace: str, cands: Candidates) -> dict:
    """-> {section: [ (score, text, [uids]) ]} items sorted best-first."""
    items = {k: [] for k in SECTIONS}
    head = f"QUESTION: {question.strip()}"
    if stack_trace:
        head += "\nSTACK TRACE (tail):\n" + "\n".join(stack_trace.strip().splitlines()[-12:])
    signals = [f"{k}={v}" for k, v in (("frames", [f"{f['path']}:{f['line']}" for f in q["frames"]]),
                                        ("errors", q["exceptions"] + q["status_codes"]),
                                        ("endpoints", q["endpoints"])) if v]
    if signals:
        head += "\nPARSED SIGNALS: " + "; ".join(signals)
    items["question"].append((1e9, head, []))

    roots = load_roots(project)
    comps = [c for c in cands.by_label("Component") if c["hop"] <= 1]
    for c in sorted(comps, key=lambda c: c["hop"]):
        n = c["node"]
        role = "seed" if c["hop"] == 0 else ("co-changed with seed" if c.get("cochange") else "dependency neighbour")
        seed_files = [f["node"]["path"] for f in cands.by_label("File") if f.get("seed")
                      and component_key(f["node"]["path"], roots) == n["path"]]
        line = f"- {_cite(n['uid'])} component `{n['path'] or '(root)'}` ({role})"
        if seed_files:
            line += " — relevant files: " + ", ".join(f"{_cite(uid('file', project, p))} {p}" for p in seed_files)
        elif c.get("why") == "repo overview":
            files = run("""MATCH (:Component {uid: $u})-[:CONTAINS]->(f:File) WHERE NOT coalesce(f.deleted, false)
                           OPTIONAL MATCH (f)-[:DEFINES]->(s:Symbol)
                           RETURN f.uid AS uid, f.path AS path, collect(s.kind + ' ' + s.name)[..6] AS syms
                           ORDER BY f.path LIMIT 20""", u=n["uid"])
            for f in files:
                line += f"\n  - {_cite(f['uid'])} {f['path']}" + (f" ({', '.join(f['syms'])})" if f["syms"] else "")
        items["components"].append((10 - c["hop"], line, [n["uid"]]))

    # Symbols and recent history of the seed files: lets any repo answer "how" and "why" without ADRs.
    seed_file_ids = [f["node"]["uid"] for f in sorted(cands.by_label("File"), key=lambda f: f["hop"])
                     if f.get("seed") and f["node"].get("language")][:8]
    for r in run("""MATCH (f:File)-[:DEFINES]->(s:Symbol) WHERE f.uid IN $ids
                    WITH f, s ORDER BY s.line
                    RETURN f.uid AS uid, f.path AS path, collect(s.kind + ' ' + s.name + ':' + s.line)[..14] AS syms""",
                 ids=seed_file_ids):
        items["code"].append((5.0, f"- {_cite(r['uid'])} {r['path']}: " + ", ".join(r["syms"]), [r["uid"]]))
    for r in run("""MATCH (c:Commit)-[m:MODIFIED]->(f:File) WHERE f.uid IN $ids
                    WITH c, collect(f.path)[..3] AS paths ORDER BY c.ts DESC LIMIT 10
                    RETURN c.uid AS uid, c.short AS short, c.ts AS ts, c.msg AS msg, c.author AS author, paths""",
                 ids=seed_file_ids):
        msg = " ".join(r["msg"].split())[:220]
        items["history"].append((r["ts"] / 1e10, f"- {_cite(r['uid'])} {r['short']} ({_date(r['ts'])}, {r['author']}) "
                                                f"[{', '.join(r['paths'])}]: {msg}", [r["uid"]]))

    ids = list(cands.items)
    for r in run("""MATCH (d:Decision) WHERE d.uid IN $ids
                    OPTIONAL MATCH (d)-[:IMPLEMENTED_BY]->(cm:Commit)
                    OPTIONAL MATCH (d)-[:AFFECTS]->(c:Component)
                    OPTIONAL MATCH (d)-[:SUPERSEDES]->(old:Decision)
                    OPTIONAL MATCH (new:Decision)-[:SUPERSEDES]->(d)
                    RETURN d, cm, collect(DISTINCT c.path) AS comps, collect(DISTINCT old.uid) AS olds,
                           collect(DISTINCT new.uid) AS news""", ids=ids):
        d, cm, c = r["d"], r["cm"], cands.items[r["d"]["uid"]]
        line = f"- {_cite(d['uid'])} DECISION ({_date(d['ts'])}, source={d.get('source')}"
        line += ", ANCHOR: earliest decision on this component)" if c.get("anchor") else ")"
        line += f": {d['summary']}"
        if d.get("reason"):
            line += f"\n  Reason: {d['reason']}"
        if r["comps"]:
            line += f"\n  Affects: {', '.join(p or '(root)' for p in r['comps'])}"
        uids = [d["uid"]]
        if cm:
            line += f"\n  Implemented by {_cite(cm['uid'])} commit {cm['short']} by {cm['author']}"
            uids.append(cm["uid"])
        if r["olds"]:
            line += "\n  Supersedes " + ", ".join(_cite(u) for u in r["olds"])
        if r["news"]:
            line += "\n  SUPERSEDED BY " + ", ".join(_cite(u) for u in r["news"])
        items["decisions"].append((float("inf") if c.get("anchor") else c["score"], line, uids))

    bug_ids = {c["node"]["uid"]: c["score"] for c in cands.by_label("Bug")}
    for f in cands.by_label("Fix"):
        for r in run("MATCH (:Fix {uid: $u})-[:RESOLVES]->(b:Bug) RETURN b.uid AS b", u=f["node"]["uid"]):
            bug_ids[r["b"]] = max(bug_ids.get(r["b"], 0), f["score"])
    for r in run("""MATCH (b:Bug) WHERE b.uid IN $ids
                    OPTIONAL MATCH (b)-[:OCCURRED_IN]->(c:Component)
                    OPTIONAL MATCH (fx:Fix)-[:RESOLVES]->(b)
                    OPTIONAL MATCH (fx)-[:IMPLEMENTED_BY]->(cm:Commit)
                    OPTIONAL MATCH (t:TestRun)-[:VALIDATES]->(fx)
                    OPTIONAL MATCH (b)-[s:SIMILAR_TO]-(b2:Bug)
                    RETURN b, collect(DISTINCT c.path) AS comps,
                           collect(DISTINCT {fx: fx, cm: cm, t: t}) AS fixes,
                           collect(DISTINCT {uid: b2.uid, score: s.score}) AS similar""", ids=list(bug_ids)):
        b = r["b"]
        line = f"- {_cite(b['uid'])} BUG ({_date(b['ts'])}): {b['title']}"
        if b.get("error_signature"):
            line += f"\n  Error: {b['error_signature']}"
        if r["comps"]:
            line += f"\n  In: {', '.join(p or '(root)' for p in r['comps'])}"
        uids = [b["uid"]]
        fixes = [x for x in r["fixes"] if x["fx"]]
        for x in fixes:
            fx = x["fx"]
            line += f"\n  FIX {_cite(fx['uid'])} ({_date(fx['ts'])}): {fx['summary']} — verified={str(bool(fx.get('verified'))).lower()}"
            uids.append(fx["uid"])
            if x["cm"]:
                line += f" via {_cite(x['cm']['uid'])} commit {x['cm']['short']}"
                uids.append(x["cm"]["uid"])
            if x["t"]:
                line += f"; validated by {_cite(x['t']['uid'])} `{x['t']['command']}` passed={x['t']['passed']}"
                uids.append(x["t"]["uid"])
        if not fixes:
            line += "\n  No fix recorded (OPEN)."
        sims = [x for x in r["similar"] if x["uid"]]
        if sims:
            line += "\n  Similar to " + ", ".join(f"{_cite(x['uid'])} ({x['score']:.2f})" for x in sims)
        items["bugs"].append((bug_ids[b["uid"]], line, uids))

    for r in run("""MATCH (fa:FailedAttempt) WHERE fa.uid IN $ids
                    OPTIONAL MATCH (fa)-[:ATTEMPTED_FOR]->(b:Bug)
                    OPTIONAL MATCH (fa)-[:IMPLEMENTED_BY]->(cm:Commit)
                    RETURN fa, b.uid AS bug, cm""", ids=ids):
        fa = r["fa"]
        why = " ".join(fa["why_failed"].split())
        line = f"- {_cite(fa['uid'])} FAILED ATTEMPT ({_date(fa['ts'])}): {fa['what']}\n  Why it failed: {why}"
        uids = [fa["uid"]]
        if r["bug"]:
            line += f"\n  Attempted for {_cite(r['bug'])}"
        if r["cm"]:
            line += f"\n  Reverted commit {_cite(r['cm']['uid'])} {r['cm']['short']}"
            uids.append(r["cm"]["uid"])
        items["failed_attempts"].append((cands.items[fa["uid"]]["score"], line, uids))

    dep_seen = set()
    for c in cands.by_label("Commit"):
        for b in c.get("bumps", []):
            if (b["dep"], c["node"]["uid"]) in dep_seen:
                continue
            dep_seen.add((b["dep"], c["node"]["uid"]))
            dep = cands.items.get(b["dep"])
            name = dep["node"]["name"] if dep else b["dep"]
            n = c["node"]
            line = (f"- {_cite(b['dep'])} {name} bumped {b['from']} -> {b['to']} in {_cite(n['uid'])} "
                    f"commit {n['short']} ({_date(n['ts'])}): {n['msg'].splitlines()[0]}")
            items["dependency_changes"].append((c["score"], line, [b["dep"], n["uid"]]))
    bumped = {d for d, _ in dep_seen}
    for d in cands.by_label("Dependency"):
        if d["node"]["uid"] not in bumped and (d["hop"] == 0 or d.get("fulltext")):
            n = d["node"]
            items["dependency_changes"].append(
                (d["score"] * 0.5, f"- {_cite(n['uid'])} {n['name']} current version {n.get('current_version', '?')}", [n["uid"]]))

    pairs = set()
    for f in sorted(cands.by_label("File"), key=lambda f: -f["score"]):
        if f.get("cochange"):
            n = f["node"]
            pair = frozenset((n["path"], f["partner"]))
            if pair in pairs:
                continue
            pairs.add(pair)
            line = f"- {_cite(n['uid'])} {n['path']} co-changed {f['cochange']}x with {f['partner']}"
            items["coupled_files"].append((f["score"], line, [n["uid"]]))

    for k in items:
        items[k].sort(key=lambda x: -x[0])
    return items


TITLES = {"code": "CODE (symbols in the relevant files)", "history": "RECENT CHANGES TO THESE FILES (commit messages)",
          "components": "SEED COMPONENTS", "decisions": "DECISIONS (with reasons)", "bugs": "BUGS + FIXES",
          "failed_attempts": "FAILED ATTEMPTS (do not repeat)", "dependency_changes": "DEPENDENCY CHANGES",
          "coupled_files": "COUPLED FILES (change together)"}


def compile_package(items: dict, budget: int) -> dict:
    parts, used, included, omitted, trimmed, uids = [], 0, [], [], {}, set()
    for sec in SECTIONS:
        sec_items = items[sec]
        if not sec_items:
            continue
        header = "" if sec == "question" else f"\n## {TITLES[sec]}\n"
        kept = []
        cost = count_tokens(header)
        for _, text, ids in sec_items:
            t = count_tokens(text + "\n")
            if used + cost + t > budget:
                continue  # trim within section: skip items that do not fit, try smaller ones
            kept.append((text, ids))
            cost += t
        if not kept:
            omitted.append(sec)
            continue
        if len(kept) < len(sec_items):
            trimmed[sec] = len(sec_items) - len(kept)
        parts.append(header + "\n".join(t for t, _ in kept))
        for _, ids in kept:
            uids.update(ids)
        used += cost
        included.append(sec)
    text = "\n".join(parts).strip()
    return {"package_text": text, "tokens": count_tokens(text), "included_sections": included,
            "omitted_sections": omitted, "trimmed": trimmed, "package_uids": sorted(uids)}


def repo_tokens_estimate(project: str) -> int:
    r = run("MATCH (p:Project {uid: $u}) RETURN p.source_chars AS c", u=uid("project", project, project))
    return int((r[0]["c"] or 0) / 4) if r else 0


def retrieve(project: str, question: str, stack_trace: str = "", budget: int = 1500) -> dict:
    try:
        return _retrieve(project, question, stack_trace, budget)
    except (SessionExpired, ServiceUnavailable):  # Aura dropped an idle connection; the retry gets a fresh one
        return _retrieve(project, question, stack_trace, budget)


def _retrieve(project: str, question: str, stack_trace: str = "", budget: int = 1500) -> dict:
    q = parse(project, question, stack_trace)
    with session() as s:
        seed_files, comp_dist, cands = find_seeds(project, q, s)
        expand(project, q, seed_files, comp_dist, cands, s)
    now_ts = (run("MATCH (c:Commit {project: $p}) RETURN max(c.ts) AS t", p=project) or [{"t": 0}])[0]["t"] or 0
    score(cands, q, now_ts)
    items = build_sections(project, q, question, stack_trace, cands)
    pkg = compile_package(items, budget)
    included = set(pkg["package_uids"]) | {c for c in comp_dist if comp_dist[c] == 0}
    evidence = [c["path"] for u, c in cands.items.items() if u in included and c.get("path")]
    return {**pkg, "parsed": q, "evidence_paths": evidence,
            "candidates_considered": len(cands.items), "repo_tokens_estimate": repo_tokens_estimate(project)}
