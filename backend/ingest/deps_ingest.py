"""Dependency versions from manifests, and BUMPED edges from manifest history."""
import json
import re
import tomllib
from pathlib import Path, PurePosixPath

from git import Repo

from graph import run, uid, write_batch

# PyPI name -> import name, for the common cases where they differ.
IMPORT_ALIASES = {"pyjwt": "jwt", "pyyaml": "yaml", "python-jose": "jose", "beautifulsoup4": "bs4",
                  "scikit-learn": "sklearn", "pillow": "PIL", "python-dotenv": "dotenv",
                  "opencv-python": "cv2", "python-multipart": "multipart", "gitpython": "git"}
REQ_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[^\]]*\])?\s*(?:(==|>=|<=|~=|!=|>|<)\s*([^\s;,#]+))?")


def is_manifest(path: str) -> bool:
    name = PurePosixPath(path).name
    return name == "package.json" or name == "pyproject.toml" or (name.startswith("requirements") and name.endswith(".txt"))


def parse_manifest(path: str, text: str) -> dict[tuple[str, str], str]:
    """-> {(ecosystem, name): version}"""
    out = {}
    name = PurePosixPath(path).name
    try:
        if name == "package.json":
            data = json.loads(text)
            for sec in ("dependencies", "devDependencies", "peerDependencies"):
                for k, v in (data.get(sec) or {}).items():
                    out[("npm", k)] = str(v).lstrip("^~=v ")
        elif name == "pyproject.toml":
            data = tomllib.loads(text)
            for spec in data.get("project", {}).get("dependencies", []):
                m = REQ_RE.match(spec)
                if m:
                    out[("pypi", m[1].lower())] = m[3] or "*"
            poetry = data.get("tool", {}).get("poetry", {}).get("dependencies", {})
            for k, v in poetry.items():
                if k.lower() != "python":
                    ver = v if isinstance(v, str) else (v.get("version", "*") if isinstance(v, dict) else "*")
                    out[("pypi", k.lower())] = ver.lstrip("^~=")
        else:
            for line in text.splitlines():
                if line.strip().startswith(("#", "-")):
                    continue
                m = REQ_RE.match(line)
                if m:
                    out[("pypi", m[1].lower())] = m[3] or "*"
    except Exception:
        pass
    return out


def import_name(eco: str, name: str) -> str:
    if eco == "npm":
        return name
    return IMPORT_ALIASES.get(name, name.replace("-", "_"))


def _show(repo: Repo, sha: str, path: str) -> str:
    try:
        return repo.git.show(f"{sha}:{path}")
    except Exception:
        return ""


def write_bumps(project: str, repo_dir: Path, commits: list[dict]) -> int:
    repo = Repo(repo_dir)
    bumps = []
    for c in commits:
        for f in c["files"]:
            if not is_manifest(f["path"]):
                continue
            new = parse_manifest(f["path"], _show(repo, c["sha"], f["path"]))
            old = parse_manifest(f["path"], _show(repo, c["parents"][0], f["path"])) if c["parents"] else {}
            for key, ver in new.items():
                if key in old and old[key] != ver:
                    bumps.append({"c": uid("commit", project, c["sha"]), "d": uid("dep", project, f"{key[0]}:{key[1]}"),
                                  "eco": key[0], "name": key[1], "imp": import_name(*key), "from": old[key], "to": ver})
    write_batch("""UNWIND $rows AS row
        MATCH (c:Commit {uid: row.c})
        MERGE (d:Dependency {uid: row.d})
        ON CREATE SET d.name = row.name, d.ecosystem = row.eco, d.import_name = row.imp, d.project = $p
        MERGE (c)-[b:BUMPED]->(d) SET b.from = row.from, b.to = row.to""", bumps, p=project)
    return len(bumps)


def write_current_deps(project: str, repo_dir: Path, manifest_paths: list[str]) -> int:
    rows = {}
    for p in manifest_paths:
        for (eco, name), ver in parse_manifest(p, (repo_dir / p).read_text("utf8", "replace")).items():
            rows[(eco, name)] = {"uid": uid("dep", project, f"{eco}:{name}"), "name": name, "eco": eco,
                                 "ver": ver, "imp": import_name(eco, name)}
    write_batch("""UNWIND $rows AS row
        MERGE (d:Dependency {uid: row.uid})
        SET d.name = row.name, d.ecosystem = row.eco, d.current_version = row.ver,
            d.import_name = row.imp, d.project = $p""", list(rows.values()), p=project)
    # Component USES Dependency when any of its files imports it.
    run("""MATCH (c:Component {project: $p})-[:CONTAINS]->(f:File), (d:Dependency {project: $p})
           WHERE d.import_name IN coalesce(f.ext_imports, [])
           MERGE (c)-[:USES]->(d)""", p=project)
    return len(rows)


def ingest_deps(project: str, repo_dir: Path, file_paths: list[str], commits: list[dict], log=print):
    n_cur = write_current_deps(project, repo_dir, [p for p in file_paths if is_manifest(p)])
    n_b = write_bumps(project, repo_dir, commits)
    log(f"deps: {n_cur} dependencies, {n_b} version bumps")
