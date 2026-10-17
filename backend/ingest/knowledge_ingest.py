"""Decisions, bugs, fixes, failed attempts. The only LLM use at ingestion: batch commit classification.

Reverts are detected deterministically (git's "This reverts commit <sha>"), ADRs and HACK/FIXME/NOTE
comments are parsed without an LLM. If no Groq key is set, a keyword heuristic stands in for the LLM.
"""
import json
import os
import re
import threading
from pathlib import Path

import numpy as np

from graph import run, uid, write_batch

BATCH = 20
KNOWLEDGE_MODEL = os.getenv("KNOWLEDGE_MODEL", "groq/llama-3.3-70b-versatile")
REVERT_RE = re.compile(r"This reverts commit ([0-9a-f]{7,40})")
COMMENT_RE = re.compile(r"(?:#|//)\s*(HACK|FIXME|NOTE|XXX)\b[:\s]+(.+)")
ERR_RE = re.compile(r"\b([1-5]\d\d)\b|\b([A-Z][A-Za-z]+(?:Error|Exception))\b")

_model = None
_model_lock = threading.Lock()


def embed(texts: list[str]) -> np.ndarray:
    global _model
    with _model_lock:
        if _model is None:
            from sentence_transformers import SentenceTransformer
            _model = SentenceTransformer("all-MiniLM-L6-v2")
    return _model.encode(texts, normalize_embeddings=True)


# ---------- classification ----------

SYSTEM = """You classify git commits for a project knowledge graph.
Return JSON: {"commits": [ ...one object per input commit, same order... ]}
Each object: {"id": <input id>, "type": "decision"|"fix"|"revert"|"refactor"|"noise",
 "summary": "<= 15 words, what changed",
 "reason": "why, only if stated or clearly implied by the message; else empty string",
 "bug_title": "for fixes: the bug that was fixed, else empty",
 "error_signature": "for fixes: the concrete error/symptom, e.g. '401 Unauthorized on /api/x' or 'KeyError: user_id', else empty"}
decision = an intentional design, architecture, library or protocol choice.
fix = repairs a bug. Version bumps with no stated reason are refactor. Formatting, typos, merges are noise."""


def _heuristic(item: dict) -> dict:
    msg = item["msg"]
    first = msg.splitlines()[0] if msg else ""
    low = msg.lower()
    out = {"id": item["id"], "type": "noise", "summary": first[:120], "reason": "", "bug_title": "", "error_signature": ""}
    m = re.search(r"\b(?:because|so that|since)\b(.+)", msg, re.I | re.S)
    if m:
        out["reason"] = m[1].strip().split("\n\n")[0][:300]
    if low.startswith("revert"):
        out["type"] = "revert"
    elif re.search(r"^(fix|bugfix|hotfix)\b|\bfix(es|ed)?\b|\bcloses #\d+|\bresolves? #\d+", low):
        out["type"] = "fix"
        out["bug_title"] = re.sub(r"^(fix|bugfix|hotfix)(\([^)]*\))?:\s*", "", first, flags=re.I)
        out["bug_title"] = re.sub(r"\s*\((closes|fixes|resolves) #\d+\)", "", out["bug_title"], flags=re.I)
        errs = [a or b for a, b in ERR_RE.findall(msg)]
        out["error_signature"] = " ".join(dict.fromkeys(errs)) or out["bug_title"]
    elif re.search(r"\b(switch|migrate|adopt|replace|move to|decid|introduce|use)\b", low) and out["reason"]:
        out["type"] = "decision"
    elif re.search(r"\b(refactor|bump|upgrade|cleanup|rename)\b", low):
        out["type"] = "refactor"
    return out


def classify(items: list[dict], log=print) -> dict[str, dict]:
    use_llm = bool(os.getenv("GROQ_API_KEY")) or not KNOWLEDGE_MODEL.startswith("groq/")
    results = {}
    if not use_llm:
        log("knowledge: no GROQ_API_KEY set, using keyword heuristic classifier")
    for i in range(0, len(items), BATCH):
        batch = items[i:i + BATCH]
        parsed = None
        if use_llm:
            try:
                import litellm
                payload = [{"id": it["id"], "message": it["msg"][:600], "files": it["files"][:8]} for it in batch]
                resp = litellm.completion(
                    model=KNOWLEDGE_MODEL, temperature=0, response_format={"type": "json_object"},
                    messages=[{"role": "system", "content": SYSTEM},
                              {"role": "user", "content": json.dumps(payload)}])
                parsed = json.loads(resp.choices[0].message.content)["commits"]
            except Exception as e:
                log(f"knowledge: LLM batch failed ({e.__class__.__name__}), heuristic fallback")
        by_id = {str(p.get("id")): p for p in (parsed or []) if isinstance(p, dict)}
        for it in batch:
            results[it["id"]] = by_id.get(it["id"]) or _heuristic(it)
        log(f"knowledge: classified {min(i + BATCH, len(items))}/{len(items)}")
    return results


# ---------- ADRs, changelog, comments ----------

def _adr(text: str) -> tuple[str, str]:
    title = next((l.lstrip("# ").strip() for l in text.splitlines() if l.startswith("#")), "")
    title = re.sub(r"^(ADR[-\s]?\d+|\d+)[:.\s-]*", "", title, flags=re.I).strip()
    sections = re.split(r"^##\s+", text, flags=re.M)
    keep = [s for s in sections[1:] if s.lower().startswith(("context", "decision", "consequences", "rationale"))]
    reason = " ".join(w for s in keep for w in " ".join(s.splitlines()[1:]).split())
    return title, reason[:700]


def _mentioned_components(text: str, comps: list[dict]) -> list[str]:
    low = text.lower()
    return [c["uid"] for c in comps if len(c["name"]) >= 3 and re.search(rf"\b{re.escape(c['name'].lower())}\b", low)]


def ingest_knowledge(project: str, repo_dir: Path, file_paths: list[str], commits: list[dict], log=print):
    comps = run("MATCH (c:Component {project: $p}) RETURN c.uid AS uid, c.name AS name", p=project)
    by_sha = {c["sha"]: c for c in commits}
    head_ts = commits[0]["ts"] if commits else 0

    # 1. Reverts (deterministic). The reverted commit becomes a FailedAttempt.
    failed = {}
    for c in commits:
        m = REVERT_RE.search(c["msg"])
        if m:
            orig = next((s for s in by_sha if s.startswith(m[1])), None)
            if orig:
                why = REVERT_RE.sub("", "\n".join(c["msg"].splitlines()[1:])).strip(" .\n") or f"Reverted in {c['sha'][:7]}"
                failed[orig] = {"revert": c, "why": why, "what": by_sha[orig]["msg"].splitlines()[0]}

    # 2. ADRs, linked to the commit that added them.
    adr_by_sha = {}
    adr_rows = []
    for p in file_paths:
        if re.match(r"(doc|docs)/(adr|adrs|decisions)/.+\.md$", p, re.I):
            title, reason = _adr((repo_dir / p).read_text("utf8", "replace"))
            adding = [c for c in commits if any(f["path"] == p for f in c["files"])]
            first = adding[-1] if adding else None
            row = {"uid": uid("decision", project, p), "summary": title, "reason": reason,
                   "ts": first["ts"] if first else head_ts, "source": "ingested_llm", "origin": p,
                   "commit": uid("commit", project, first["sha"]) if first else None,
                   "comps": _mentioned_components(title + " " + reason, comps)}
            adr_rows.append(row)
            if first:
                adr_by_sha[first["sha"]] = row

    # 3. Classify commits (+ changelog bullets) with the LLM.
    items = [{"id": c["sha"][:12], "msg": c["msg"],
              "files": [f"{f['path']} (+{f['added']}/-{f['removed']})" for f in c["files"]]}
             for c in commits if c["sha"] not in failed and not REVERT_RE.search(c["msg"]) and len(c["parents"]) < 2]
    for p in file_paths:
        if re.match(r"CHANGELOG", Path(p).name, re.I):
            bullets = [l.strip("-* ").strip() for l in (repo_dir / p).read_text("utf8", "replace").splitlines()
                       if l.strip().startswith(("-", "*"))]
            items += [{"id": f"changelog:{i}", "msg": b, "files": []} for i, b in enumerate(bullets[:60])]
    labels = classify(items, log)

    decisions, bugs, fixes = list(adr_rows), [], []
    for c in commits:
        lab = labels.get(c["sha"][:12])
        if not lab:
            continue
        cuid = uid("commit", project, c["sha"])
        t = lab.get("type")
        if t == "decision":
            if c["sha"] in adr_by_sha:  # same decision as its ADR; enrich rather than duplicate
                row = adr_by_sha[c["sha"]]
                row["reason"] = row["reason"] or lab.get("reason", "")
                continue
            decisions.append({"uid": uid("decision", project, c["sha"]), "summary": lab.get("summary") or c["msg"][:120],
                              "reason": lab.get("reason", ""), "ts": c["ts"], "source": "ingested_llm",
                              "origin": f"commit {c['sha'][:7]}", "commit": cuid, "comps": []})
        elif t == "fix":
            bug_uid = uid("bug", project, c["sha"])
            bugs.append({"uid": bug_uid, "title": lab.get("bug_title") or lab.get("summary") or c["msg"][:120],
                         "error_signature": lab.get("error_signature", ""), "ts": c["ts"], "commit": cuid, "comps": []})
            fixes.append({"uid": uid("fix", project, c["sha"]), "summary": lab.get("summary") or c["msg"][:120],
                          "ts": c["ts"], "bug": bug_uid, "commit": cuid})
    for it in items:
        if it["id"].startswith("changelog:"):
            lab = labels[it["id"]]
            if lab.get("type") == "decision":
                decisions.append({"uid": uid("decision", project, it["msg"]), "summary": lab.get("summary") or it["msg"],
                                  "reason": lab.get("reason", ""), "ts": head_ts, "source": "ingested_llm",
                                  "origin": "CHANGELOG", "commit": None, "comps": _mentioned_components(it["msg"], comps)})

    # 4. HACK/FIXME/NOTE comments.
    for p in file_paths:
        if not p.endswith((".py", ".js", ".ts", ".tsx", ".jsx")):
            continue
        for ln, line in enumerate((repo_dir / p).read_text("utf8", "replace").splitlines(), 1):
            m = COMMENT_RE.search(line)
            if not m:
                continue
            fid = uid("file", project, p)
            if m[1] in ("FIXME", "XXX"):
                bugs.append({"uid": uid("bug", project, f"{p}:{ln}"), "title": m[2].strip(), "error_signature": "",
                             "ts": head_ts, "commit": None, "comps": [], "file": fid})
            else:
                decisions.append({"uid": uid("decision", project, f"{p}:{ln}"), "summary": m[2].strip()[:200],
                                  "reason": f"{m[1]} comment in {p}:{ln}", "ts": head_ts, "source": "ingested_llm",
                                  "origin": f"{p}:{ln}", "commit": None, "comps": [], "file": fid})

    write_knowledge(project, decisions, bugs, fixes)
    write_failed(project, failed)
    n_sim = link_similar_bugs(project)
    log(f"knowledge: {len(decisions)} decisions, {len(bugs)} bugs, {len(fixes)} fixes, "
        f"{len(failed)} failed attempts, {n_sim} similar-bug links")


def write_knowledge(project, decisions, bugs, fixes):
    write_batch("""UNWIND $rows AS row
        MERGE (d:Decision {uid: row.uid})
        SET d.summary = row.summary, d.reason = row.reason, d.ts = row.ts,
            d.source = coalesce(d.source, row.source), d.origin = row.origin, d.project = $p
        WITH d, row
        OPTIONAL MATCH (cm:Commit {uid: row.commit})
        FOREACH (_ IN CASE WHEN cm IS NULL THEN [] ELSE [1] END | MERGE (d)-[:IMPLEMENTED_BY]->(cm))
        WITH d, row,
             COLLECT { MATCH (:Commit {uid: row.commit})-[:MODIFIED]->(:File)<-[:CONTAINS]-(c:Component) RETURN DISTINCT c } +
             COLLECT { MATCH (c:Component) WHERE c.uid IN row.comps RETURN c } +
             COLLECT { MATCH (:File {uid: row.file})<-[:CONTAINS]-(c:Component) RETURN c } AS cs
        FOREACH (c IN cs | MERGE (d)-[:AFFECTS]->(c))""", decisions, p=project)
    write_batch("""UNWIND $rows AS row
        MERGE (b:Bug {uid: row.uid})
        SET b.title = row.title, b.error_signature = row.error_signature, b.ts = row.ts, b.project = $p
        WITH b, row,
             COLLECT { MATCH (:Commit {uid: row.commit})-[:MODIFIED]->(:File)<-[:CONTAINS]-(c:Component) RETURN DISTINCT c } +
             COLLECT { MATCH (:File {uid: row.file})<-[:CONTAINS]-(c:Component) RETURN c } AS cs
        FOREACH (c IN cs | MERGE (b)-[:OCCURRED_IN]->(c))""", bugs, p=project)
    write_batch("""UNWIND $rows AS row
        MATCH (b:Bug {uid: row.bug}), (cm:Commit {uid: row.commit})
        MERGE (f:Fix {uid: row.uid})
        SET f.summary = row.summary, f.ts = row.ts, f.verified = coalesce(f.verified, false), f.project = $p
        MERGE (f)-[:RESOLVES]->(b)
        MERGE (f)-[:IMPLEMENTED_BY]->(cm)""", fixes, p=project)


def write_failed(project, failed: dict):
    rows = [{"uid": uid("failed", project, sha), "what": v["what"], "why": v["why"], "ts": v["revert"]["ts"],
             "commit": uid("commit", project, sha)} for sha, v in failed.items()]
    write_batch("""UNWIND $rows AS row
        MATCH (cm:Commit {uid: row.commit})
        MERGE (fa:FailedAttempt {uid: row.uid})
        SET fa.what = row.what, fa.why_failed = row.why, fa.ts = row.ts, fa.project = $p
        MERGE (fa)-[:IMPLEMENTED_BY]->(cm)
        WITH fa, cm, row
        // attach to the most recent bug (at or before the attempt) in the components the attempt touched
        OPTIONAL MATCH (cm)-[:MODIFIED]->(:File)<-[:CONTAINS]-(c:Component)<-[:OCCURRED_IN]-(b:Bug)
        WITH fa, b, row ORDER BY CASE WHEN b.ts <= row.ts THEN 0 ELSE 1 END, abs(row.ts - b.ts)
        WITH fa, head(collect(b)) AS b
        FOREACH (_ IN CASE WHEN b IS NULL THEN [] ELSE [1] END | MERGE (fa)-[:ATTEMPTED_FOR]->(b))""",
                rows, p=project)


def link_similar_bugs(project: str) -> int:
    bugs = run("""MATCH (b:Bug {project: $p})
                  RETURN b.uid AS uid, b.title + ' ' + coalesce(b.error_signature, '') AS text""", p=project)
    if not bugs:
        return 0
    vecs = embed([b["text"] for b in bugs])
    write_batch("UNWIND $rows AS row MATCH (b:Bug {uid: row.uid}) SET b.embedding = row.v",
                [{"uid": b["uid"], "v": v.tolist()} for b, v in zip(bugs, vecs)])
    sims = vecs @ vecs.T
    rows = [{"a": bugs[i]["uid"], "b": bugs[j]["uid"], "s": float(sims[i, j])}
            for i in range(len(bugs)) for j in range(i + 1, len(bugs)) if sims[i, j] > 0.8]
    write_batch("""UNWIND $rows AS row MATCH (a:Bug {uid: row.a}), (b:Bug {uid: row.b})
                   MERGE (a)-[r:SIMILAR_TO]->(b) SET r.score = row.s""", rows)
    return len(rows)
