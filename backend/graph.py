"""Neo4j driver, schema, and small write helpers. Everything else talks to Neo4j through here."""
import hashlib
import os
from pathlib import Path

from dotenv import load_dotenv
from neo4j import GraphDatabase
from neo4j.exceptions import ServiceUnavailable, SessionExpired

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

WORKSPACE_DIR = (ROOT / os.getenv("WORKSPACE_DIR", "./workspace")).resolve()
WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)

driver = GraphDatabase.driver(
    os.getenv("NEO4J_URI", "bolt://localhost:7687"),
    auth=(os.getenv("NEO4J_USER", "neo4j"), os.getenv("NEO4J_PASSWORD", "repomemory")),
    notifications_min_severity="OFF",
    # AuraDB drops idle connections; check before reuse and recycle them regularly
    liveness_check_timeout=30,
    max_connection_lifetime=300,
    keep_alive=True,
)

# AuraDB: NEO4J_URI=neo4j+s://<id>.databases.neo4j.io, NEO4J_DATABASE usually "neo4j" (or the instance id)
DATABASE = os.getenv("NEO4J_DATABASE") or None


def session():
    return driver.session(database=DATABASE)


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
    with session() as s:
        for q in SCHEMA:
            s.run(q)


def run(query: str, **params) -> list[dict]:
    for attempt in (1, 2):  # one retry if Aura dropped the connection
        try:
            with session() as s:
                return [r.data() for r in s.run(query, **params)]
        except (SessionExpired, ServiceUnavailable):
            if attempt == 2:
                raise


def write_batch(query: str, rows: list[dict], size: int = 500, **params):
    """Run `UNWIND $rows AS row ...` in chunks."""
    with session() as s:
        for i in range(0, len(rows), size):
            s.run(query, rows=rows[i:i + size], **params).consume()


def uid(kind: str, project: str, key: str) -> str:
    """Short, deterministic, globally-unique ids. Short matters: the LLM has to cite them."""
    h = hashlib.sha1(f"{project}\x00{key}".encode()).hexdigest()[:8]
    return f"{kind}:{h}"


def clear_project(project: str):
    run("MATCH (n {project: $p}) CALL { WITH n DETACH DELETE n } IN TRANSACTIONS OF 2000 ROWS", p=project)


def clear_all():
    """One repo at a time: drop every node from previous ingests (incl. legacy project-less Developers)."""
    labels = "|".join(LABELS)
    run(f"MATCH (n:{labels}) CALL {{ WITH n DETACH DELETE n }} IN TRANSACTIONS OF 2000 ROWS")
