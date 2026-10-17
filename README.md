# RepoMemory

A developer assistant with project memory. Paste a repo URL; RepoMemory builds a Neo4j knowledge graph of
components, files, dependencies, commits, decisions (with reasons), bugs, fixes and failed attempts.
Questions are answered with **deterministic, graph-first retrieval**. No LLM searches or filters anything.
Only a small, token-budgeted package goes to the LLM, and every claim must cite a node id.

> The LLM never reads your repo. It reads the 15 nodes that explain your bug.

## Run

```bash
cp .env.example .env            # add GROQ_API_KEY / ANTHROPIC_API_KEY / OPENAI_API_KEY
docker compose up -d            # Neo4j 5 + APOC (http://localhost:7474, neo4j/repomemory)

cd backend
python -m venv .venv && .venv/Scripts/pip install -r requirements.txt   # .venv/bin/pip on macOS/Linux
.venv/Scripts/python -m uvicorn app:app --host 127.0.0.1 --port 8000

cd frontend && npm install && npm run dev                                # http://127.0.0.1:5173
```

The backend must stay bound to 127.0.0.1 because `/ws/terminal` is a real local shell.

## Demo repo

```bash
bash demo-repo/build_demo_repo.sh       # creates demo-repo/shop-api with a scripted, backdated history
```

In the UI, ingest the absolute path to `demo-repo/shop-api`. Then, in the built-in terminal, create the demo venv once:
`python -m venv .venv && .venv/Scripts/pip install -r requirements.txt && source .venv/Scripts/activate`.
To restart the demo from scratch, POST `/api/ingest` with `{"repo_url": "...", "reset": true}`.

## Demo script

1. Ask: *"Why did we change auth, and could it cause this 401 on /api/orders?"* The graph highlights orders, then
   the co-changed auth files, the JWT decision (ADR 001), the pyjwt 1.7 to 2.8 bump, the past aud-mismatch bug and its
   fix, and the failed attempt ("don't disable aud verification").
2. Terminal: `pytest tests/test_orders.py`. It fails, and that run is recorded as a TestRun plus an open Bug.
3. Editor: in `app/orders/router.py`, import `AUDIENCE` from `app.auth.tokens` and use it instead of `"shop-api"`. Save with Ctrl+S.
4. Terminal: `pytest tests/test_orders.py` now passes.
5. Click **Commit + record decision**. You can draft the reason with AI, then pick the failing-test bug under "fixes bug" and commit.
   This adds a Decision, a Fix (verified by the passing TestRun) and the new Commit, and the graph updates live.
6. Ask again. The answer now cites the new, verified fix.

## Layout

- `backend/ingest/`: code (tree-sitter), git (commits, co-change), deps (BUMPED), knowledge (LLM commit classification, ADRs, comments), incremental
- `backend/retrieve/`: `parse_query.py` (regex, no LLM) and `retriever.py` (seeds, then bounded Cypher, scoring, budgeted package)
- `backend/llm.py`: LiteLLM answer call and the citation check
- `backend/workspace.py`, `backend/terminal.py`: file I/O, git, and a pty shell that captures test runs
- `frontend/`: React, NVL graph, Monaco, xterm.js
