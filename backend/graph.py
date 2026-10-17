"""Neo4j driver, schema, and small write helpers. Everything else talks to Neo4j through here."""
import hashlib
import os
from pathlib import Path

from dotenv import load_dotenv
from neo4j import GraphDatabase

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

WORKSPACE_DIR = (ROOT / os.getenv("WORKSPACE_DIR", "./workspace")).resolve()
WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)

driver = GraphDatabase.driver(
    os.getenv("NEO4J_URI", "bolt://localhost:7687"),
    auth=(os.getenv("NEO4J_USER", "neo4j"), os.getenv("NEO4J_PASSWORD", "repomemory")),
    notifications_min_severity="OFF",
)

LABELS = ["Project", "Component", "File", "Symbol", "Dependency", "Commit", "Developer",
          "Decision", "Bug", "Fix", "FailedAttempt", "TestRun"]

SCHEMA = [
    *[f"CREATE CONSTRAINT {l.lower()}_uid IF NOT EXISTS FOR (n:{l}) REQUIRE n.uid IS UNIQUE" for l in LABELS],
    """CREATE FULLTEXT INDEX entity_names IF NOT EXISTS
       FOR (n:Component|File|Symbol|Dependency) ON EACH [n.name, n.path]""",
    """CREATE FULLTEXT INDEX knowledge_text IF NOT EXISTS
       FOR (n:Decision|Bug|Fix|FailedAttempt) ON EACH [n.summary, n.reason, n.title, n.error_signature, n.what, n.why_failed]""",
    """CREATE VECTOR INDEX bug_embedding IF NOT EXISTS FOR (b:Bug) ON (b.embedding)
       OPTIONS {indexConfig: {`vector.dimensions`: 384, `vector.similarity_function`: 'cosine'}}""",
]


def init_schema():
    with driver.session() as s:
        for q in SCHEMA:
            s.run(q)


def run(query: str, **params) -> list[dict]:
    with driver.session() as s:
        return [r.data() for r in s.run(query, **params)]


def write_batch(query: str, rows: list[dict], size: int = 500, **params):
    """Run `UNWIND $rows AS row ...` in chunks."""
    with driver.session() as s:
        for i in range(0, len(rows), size):
            s.run(query, rows=rows[i:i + size], **params).consume()


def uid(kind: str, project: str, key: str) -> str:
    """Short, deterministic, globally-unique ids. Short matters: the LLM has to cite them."""
    h = hashlib.sha1(f"{project}\x00{key}".encode()).hexdigest()[:8]
    return f"{kind}:{h}"


def clear_project(project: str):
    run("MATCH (n {project: $p}) DETACH DELETE n", p=project)
