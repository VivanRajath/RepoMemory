"""FastAPI routes + websockets. Run from backend/: uvicorn app:app --port 8000"""
import asyncio
import json
import os
import re
import threading
import time
import traceback
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

import llm
import workspace
from graph import WORKSPACE_DIR, clear_all, init_schema, run, uid
from ingest.code_ingest import ingest_code, list_files
from ingest.deps_ingest import ingest_deps
from ingest.git_ingest import clone, ingest_git, repo_name
from ingest.incremental import ingest_commit
from ingest.knowledge_ingest import embed, ingest_knowledge, link_similar_bugs, write_knowledge
from retrieve.retriever import retrieve
from terminal import terminal_session

app = FastAPI(title="RepoMemory")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

STATE_FILE = WORKSPACE_DIR / ".current.json"


def current() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def set_current(project: str, repo_dir: Path, url: str):
    STATE_FILE.write_text(json.dumps({"project": project, "repo_dir": str(repo_dir), "url": url}))


def require_current() -> tuple[str, Path]:
    cur = current()
    if not cur:
        raise HTTPException(400, "No repo ingested yet")
    return cur["project"], Path(cur["repo_dir"])


# ---------- progress hub (thread-safe broadcast to /ws/progress) ----------

class Hub:
    def __init__(self):
        self.clients: set[WebSocket] = set()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.history: list[dict] = []

    def emit(self, msg: dict):
        msg = {"ts": time.time(), **msg}
        self.history = (self.history + [msg])[-200:]
        if self.loop:
            asyncio.run_coroutine_threadsafe(self._broadcast(msg), self.loop)

    def log(self, text: str):
        print(text, flush=True)
        self.emit({"type": "log", "msg": text})

    async def _broadcast(self, msg):
        for ws in list(self.clients):
            try:
                await ws.send_json(msg)
            except Exception:
                self.clients.discard(ws)


hub = Hub()
ingest_lock = threading.Lock()


@app.on_event("startup")
async def startup():
    hub.loop = asyncio.get_running_loop()
    init_schema()
    # Warm the embedding model so the first /api/ask isn't slow.
    threading.Thread(target=lambda: embed(["warmup"]), daemon=True).start()


@app.websocket("/ws/progress")
async def ws_progress(ws: WebSocket):
    await ws.accept()
    hub.clients.add(ws)
    try:
        for m in hub.history[-50:]:
            await ws.send_json(m)
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        hub.clients.discard(ws)


# ---------- ingestion ----------

class IngestReq(BaseModel):
    repo_url: str
    reset: bool = False  # wipe this project's nodes first (e.g. after rebuilding the demo repo)


def full_ingest(url: str, reset: bool = False):
    log = hub.log
    try:
        t0 = time.time()
        project = repo_name(url)
        hub.emit({"type": "start", "project": project})
        clear_all()  # the graph only ever holds the repo being ingested: no stale nodes
        log("graph: cleared previous data")
        repo_dir = clone(url, log, reset=reset)
        set_current(project, repo_dir, url)
        code = ingest_code(project, url, repo_dir, log)
        commits = ingest_git(project, repo_dir, log)
        paths = list_files(repo_dir)
        ingest_deps(project, repo_dir, paths, commits, log)
        ingest_knowledge(project, repo_dir, paths, commits, log)
        set_current(project, repo_dir, url)
        log(f"done in {time.time() - t0:.1f}s")
        hub.emit({"type": "done", "project": project, **code})
    except Exception as e:
        traceback.print_exc()
        log(f"ingest failed: {e!r}")
        hub.emit({"type": "error", "msg": str(e)})
    finally:
        ingest_lock.release()


@app.post("/api/ingest")
def api_ingest(req: IngestReq):
    if not ingest_lock.acquire(blocking=False):
        raise HTTPException(409, "Ingestion already running")
    threading.Thread(target=full_ingest, args=(req.repo_url.strip(), req.reset), daemon=True).start()
    return {"project": repo_name(req.repo_url.strip()), "status": "started"}


# ---------- ask ----------

class AskReq(BaseModel):
    question: str
    stack_trace: str = ""
    model: str = "groq"
    budget: int = 1500


@app.post("/api/ask")
def api_ask(req: AskReq):
    project, _ = require_current()
    pkg = retrieve(project, req.question, req.stack_trace, req.budget)
    if not os.getenv(llm.KEY_ENV.get(req.model, ""), ""):
        ans = {"answer": f"(No API key set for '{req.model}'. Retrieval still ran — see 'What the LLM saw'.)",
               "citations": [], "invalid_citations": [], "model": llm.MODELS.get(req.model, req.model)}
    else:
        try:
            ans = llm.answer(pkg, req.model)
        except Exception as e:
            ans = {"answer": f"LLM call failed: {e}", "citations": [], "invalid_citations": [], "model": req.model}
    return {**ans, "evidence_paths": pkg["evidence_paths"], "package_text": pkg["package_text"],
            "tokens_sent": pkg["tokens"], "repo_tokens_estimate": pkg["repo_tokens_estimate"],
            "included_sections": pkg["included_sections"], "omitted_sections": pkg["omitted_sections"],
            "trimmed": pkg["trimmed"], "package_uids": pkg["package_uids"], "parsed": pkg["parsed"],
            "candidates_considered": pkg["candidates_considered"]}


@app.get("/api/models")
def api_models():
    return llm.available_models()


# ---------- graph for the UI ----------

GRAPH_LABELS = ["Project", "Directory", "Component", "File", "Dependency", "Commit", "Developer", "Decision", "Bug", "Fix",
                "FailedAttempt", "TestRun"]


def _clean(props: dict) -> dict:
    return {k: v for k, v in props.items() if k not in ("embedding", "ext_imports")}


@app.get("/api/graph")
def api_graph():
    project, _ = require_current()
    nodes = run("""MATCH (n) WHERE n.project = $p AND any(l IN labels(n) WHERE l IN $labels)
                   AND NOT coalesce(n.deleted, false)
                   RETURN n.uid AS uid, labels(n)[0] AS label, properties(n) AS props LIMIT 1500""",
                p=project, labels=GRAPH_LABELS)
    devs = run("""MATCH (:Commit {project: $p})-[:AUTHORED_BY]->(d:Developer)
                  RETURN DISTINCT d.uid AS uid, 'Developer' AS label, properties(d) AS props""", p=project)
    nodes += devs
    ids = [n["uid"] for n in nodes]
    rels = run("""MATCH (a)-[r]->(b) WHERE a.uid IN $ids AND b.uid IN $ids
                  RETURN a.uid AS source, type(r) AS type, b.uid AS target, properties(r) AS props""", ids=ids)
    return {"nodes": [{**n, "props": _clean(n["props"])} for n in nodes], "rels": rels}


# ---------- workspace: files, diff, commit ----------

@app.get("/api/files")
def api_files():
    _, repo_dir = require_current()
    return {"files": workspace.file_tree(repo_dir), "changed": workspace.changed_files(repo_dir)}


@app.get("/api/file")
def api_file_get(path: str):
    _, repo_dir = require_current()
    return {"path": path, "content": workspace.read_file(repo_dir, path)}


class FileWrite(BaseModel):
    content: str


@app.put("/api/file")
def api_file_put(path: str, body: FileWrite):
    _, repo_dir = require_current()
    workspace.write_file(repo_dir, path, body.content)
    return {"ok": True, "changed": workspace.changed_files(repo_dir)}


@app.get("/api/diff")
def api_diff():
    _, repo_dir = require_current()
    return {"diff": workspace.diff(repo_dir), "changed": workspace.changed_files(repo_dir)}


def _touched_component_uids(project: str, paths: list[str]) -> list[str]:
    from ingest.code_ingest import component_key, load_roots
    roots = load_roots(project)
    return sorted({uid("comp", project, component_key(p, roots)) for p in paths})


@app.get("/api/commit_context")
def api_commit_context():
    project, repo_dir = require_current()
    changed = workspace.changed_files(repo_dir)
    comps = _touched_component_uids(project, changed)
    decisions = run("""MATCH (d:Decision)-[:AFFECTS]->(c:Component) WHERE c.uid IN $cs
                       AND NOT ()-[:SUPERSEDES]->(d)
                       RETURN DISTINCT d.uid AS uid, d.summary AS summary, d.ts AS ts ORDER BY d.ts""", cs=comps)
    bugs = run("""MATCH (b:Bug {project: $p}) WHERE NOT ()-[:RESOLVES]->(b)
                  OPTIONAL MATCH (b)-[:OCCURRED_IN]->(c:Component)
                  WITH b, collect(c.uid) AS cs
                  RETURN b.uid AS uid, b.title AS title, b.ts AS ts, any(x IN cs WHERE x IN $cs) AS on_touched
                  ORDER BY on_touched DESC, b.ts DESC""", p=project, cs=comps)
    return {"diff": workspace.diff(repo_dir), "changed": changed, "decisions": decisions, "open_bugs": bugs}


class DraftReq(BaseModel):
    model: str = "groq"


@app.post("/api/draft")
def api_draft(req: DraftReq):
    project, repo_dir = require_current()
    changed = workspace.changed_files(repo_dir)
    if not changed:
        raise HTTPException(400, "Nothing to commit")
    pkg = retrieve(project, f"Changes to {', '.join(changed)}: what decisions and bugs relate to these files?",
                   budget=1200)
    if not os.getenv(llm.KEY_ENV.get(req.model, ""), ""):
        raise HTTPException(400, f"No API key for {req.model}")
    return llm.draft_decision(workspace.diff(repo_dir), pkg["package_text"], req.model)


class CommitReq(BaseModel):
    summary: str
    reason: str = ""
    source: str = "human"  # human | llm_confirmed
    supersedes: list[str] = []
    fixes_bug: str | None = None
    new_bug_title: str | None = None


@app.post("/api/commit")
def api_commit(req: CommitReq):
    project, repo_dir = require_current()
    if not workspace.changed_files(repo_dir):
        raise HTTPException(400, "Nothing to commit")
    if not ingest_lock.acquire(blocking=False):
        raise HTTPException(409, "Ingestion running")
    try:
        sha = workspace.commit(repo_dir, req.summary.strip() + ("\n\n" + req.reason.strip() if req.reason.strip() else ""))
        res = ingest_commit(project, repo_dir, sha, hub.log)
        cm_uid, ts = res["commit_uid"], res["commit"]["ts"]
        new_uids = [cm_uid] + res["file_uids"]

        dec_uid = uid("decision", project, sha)
        source = req.source if req.source in ("human", "llm_confirmed") else "human"
        write_knowledge(project, [{"uid": dec_uid, "summary": req.summary, "reason": req.reason, "ts": ts,
                                   "source": source, "origin": f"commit {sha[:7]}", "commit": cm_uid, "comps": []}], [], [])
        new_uids.append(dec_uid)
        if req.supersedes:
            run("""MATCH (d:Decision {uid: $d}) UNWIND $olds AS o MATCH (old:Decision {uid: o})
                   MERGE (d)-[:SUPERSEDES]->(old)""", d=dec_uid, olds=req.supersedes)

        bug_uid = req.fixes_bug
        if not bug_uid and req.new_bug_title:
            bug_uid = uid("bug", project, f"manual:{sha}")
            write_knowledge(project, [], [{"uid": bug_uid, "title": req.new_bug_title, "error_signature": "",
                                           "ts": ts, "commit": cm_uid, "comps": []}], [])
            link_similar_bugs(project)
        verified = False
        if bug_uid:
            fix_uid = uid("fix", project, sha)
            write_knowledge(project, [], [], [{"uid": fix_uid, "summary": req.summary, "ts": ts, "bug": bug_uid,
                                               "commit": cm_uid}])
            # The fixing commit's components are also where the bug lived.
            run("""MATCH (b:Bug {uid: $b}), (:Commit {uid: $c})-[:MODIFIED]->(:File)<-[:CONTAINS]-(comp:Component)
                   MERGE (b)-[:OCCURRED_IN]->(comp)""", b=bug_uid, c=cm_uid)
            # Passing test runs since the last commit validate the fix.
            v = run("""MATCH (t:TestRun {project: $p, pending: true, passed: true}), (fx:Fix {uid: $f})
                       MERGE (t)-[:VALIDATES]->(fx) SET fx.verified = true RETURN t.uid AS t""", p=project, f=fix_uid)
            verified = bool(v)
            new_uids += [bug_uid, fix_uid] + [r["t"] for r in v]
        run("MATCH (t:TestRun {project: $p, pending: true}) SET t.pending = false", p=project)
        hub.emit({"type": "nodes", "uids": new_uids, "reason": "commit"})
        hub.log(f"commit {sha[:7]}: decision recorded ({source})" + (f", fix verified={verified}" if bug_uid else ""))
        return {"sha": sha, "new_uids": new_uids, "verified": verified}
    finally:
        ingest_lock.release()


# ---------- terminal + test capture ----------

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07|\r")


def on_test_command(cmd: str, code: int, output: str):
    cur = current()
    if not cur:
        return
    project, repo_dir = cur["project"], Path(cur["repo_dir"])
    text = ANSI_RE.sub("", output)
    passed = code == 0
    ts = int(time.time())
    tr_uid = uid("testrun", project, f"{cmd}:{ts}")
    run("""CREATE (t:TestRun {uid: $u, command: $cmd, passed: $passed, ts: $ts, project: $p, pending: true,
                              exit_code: $code, output_tail: $tail, changed_files: $changed})""",
        u=tr_uid, cmd=cmd, passed=passed, ts=ts, p=project, code=code, tail=text[-1500:],
        changed=workspace.changed_files(repo_dir))
    new = [tr_uid]
    if not passed:
        failed = re.findall(r"^FAILED (\S+)", text, re.M)
        errs = [l[1:].strip() for l in text.splitlines() if l.startswith("E ")][:4]
        title = f"Failing: {failed[0] if failed else cmd}"
        sig = " | ".join(errs) or (text.strip().splitlines() or [""])[-1]
        existing = run("""MATCH (b:Bug {project: $p, title: $t}) WHERE NOT ()-[:RESOLVES]->(b) RETURN b.uid AS u""",
                       p=project, t=title)
        bug_uid = existing[0]["u"] if existing else uid("bug", project, f"test:{title}:{ts}")
        # Components: the test file's own, plus components named in the test file name (test_orders -> orders).
        test_files = {f.split("::")[0] for f in failed} | set(re.findall(r"[\w/.-]+\.py", cmd))
        names = {re.sub(r"^test_|_test$", "", Path(f).stem) for f in test_files}
        run("""MERGE (b:Bug {uid: $u}) SET b.title = $t, b.error_signature = $sig, b.ts = $ts, b.project = $p
               WITH b MATCH (t:TestRun {uid: $tr}) MERGE (t)-[:REPRODUCES]->(b)
               WITH b MATCH (c:Component {project: $p})
               WHERE c.name IN $names OR EXISTS { MATCH (c)-[:CONTAINS]->(x:File) WHERE x.path IN $files }
               MERGE (b)-[:OCCURRED_IN]->(c)""",
            u=bug_uid, t=title, sig=sig[:500], ts=ts, p=project, tr=tr_uid, names=list(names), files=list(test_files))
        link_similar_bugs(project)
        new.append(bug_uid)
    hub.emit({"type": "testrun", "uid": tr_uid, "command": cmd, "passed": passed})
    hub.emit({"type": "nodes", "uids": new, "reason": "testrun"})
    hub.log(f"test run `{cmd}` -> {'PASSED' if passed else 'FAILED'}")


@app.websocket("/ws/terminal")
async def ws_terminal(ws: WebSocket):
    # Local demo shell only: refuse anything that is not loopback.
    if ws.client and ws.client.host not in ("127.0.0.1", "::1", "localhost"):
        await ws.close(code=1008)
        return
    cur = current()
    if not cur:
        await ws.accept()
        await ws.send_text("No repo ingested yet.\r\n")
        await ws.close()
        return
    await terminal_session(ws, Path(cur["repo_dir"]), on_test_command)


@app.get("/api/state")
def api_state():
    cur = current()
    if not cur:
        return {}
    stats = run("""MATCH (n {project: $p}) RETURN labels(n)[0] AS label, count(*) AS n""", p=cur["project"])
    return {**cur, "counts": {r["label"]: r["n"] for r in stats}}
