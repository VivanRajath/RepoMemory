"""Clone, commits, developers, MODIFIED line counts, CO_CHANGED."""
import re
from collections import Counter
from itertools import combinations
from pathlib import Path

from git import Repo

from graph import WORKSPACE_DIR, run, uid, write_batch

MAX_COMMITS = 500
MAX_FILES_FOR_COCHANGE = 30  # huge commits (formatting, vendoring) say nothing about coupling


def repo_name(url: str) -> str:
    return re.sub(r"\.git$", "", url.rstrip("/\\").replace("\\", "/").split("/")[-1])


def clone(url: str, log=print, reset: bool = False) -> Path:
    dest = WORKSPACE_DIR / repo_name(url)
    if (dest / ".git").exists():
        if reset:  # back to the remote's state, but keep a local .venv
            repo = Repo(dest)
            repo.remotes.origin.fetch()
            repo.git.reset("--hard", "origin/HEAD")
            repo.git.clean("-fdx", "-e", ".venv")
            log(f"git: reset clone at {dest} to origin")
        else:
            log(f"git: using existing clone at {dest}")
        return dest
    log(f"git: cloning {url}")
    # history beyond MAX_COMMITS is never read, so don't download it
    Repo.clone_from(url, dest, multi_options=[f"--depth={MAX_COMMITS}", "--no-single-branch"] if "://" in url else [])
    return dest


def read_commits(repo_dir: Path, rev: str = "HEAD", max_count: int = MAX_COMMITS) -> list[dict]:
    """One `git log --numstat` call; much faster than GitPython's per-commit stats."""
    repo = Repo(repo_dir)
    raw = repo.git.log(rev, f"-n{max_count}", "--no-renames", "--numstat",
                       "--format=%x1e%H%x1f%an%x1f%ae%x1f%at%x1f%P%x1f%B%x1d")
    commits = []
    for rec in raw.split("\x1e")[1:]:
        head, _, stat = rec.partition("\x1d")
        sha, name, email, ts, parents, body = head.split("\x1f")
        files = []
        for line in stat.strip().splitlines():
            parts = line.split("\t")
            if len(parts) == 3:
                a, r, p = parts
                files.append({"path": p, "added": int(a) if a.isdigit() else 0,
                              "removed": int(r) if r.isdigit() else 0})
        commits.append({"sha": sha, "author": name, "email": email, "ts": int(ts),
                        "parents": parents.split(), "msg": body.strip(), "files": files})
    return commits


def write_commits(project: str, commits: list[dict]):
    rows = [{"uid": uid("commit", project, c["sha"]), "sha": c["sha"], "msg": c["msg"], "ts": c["ts"],
             "author": c["author"], "dev": uid("dev", project, c["email"].lower()), "email": c["email"]}
            for c in commits]
    write_batch("""UNWIND $rows AS row
        MERGE (c:Commit {uid: row.uid})
        SET c.sha = row.sha, c.short = left(row.sha, 7), c.msg = row.msg, c.ts = row.ts,
            c.author = row.author, c.project = $p
        MERGE (d:Developer {uid: row.dev}) SET d.name = row.author, d.email = row.email, d.project = $p
        MERGE (c)-[:AUTHORED_BY]->(d)""", rows, p=project)
    mods = [{"c": uid("commit", project, c["sha"]), "f": uid("file", project, f["path"]), "path": f["path"],
             "added": f["added"], "removed": f["removed"]} for c in commits for f in c["files"]]
    # Files that no longer exist still get nodes (marked deleted) so history stays connected.
    write_batch("""UNWIND $rows AS row
        MATCH (c:Commit {uid: row.c})
        MERGE (f:File {uid: row.f}) ON CREATE SET f.path = row.path, f.name = row.path, f.project = $p, f.deleted = true
        MERGE (c)-[m:MODIFIED]->(f) SET m.added = row.added, m.removed = row.removed""", mods, p=project)


def write_cochange(project: str, commits: list[dict]):
    pairs = Counter()
    for c in commits:
        paths = sorted({f["path"] for f in c["files"]})
        if 2 <= len(paths) <= MAX_FILES_FOR_COCHANGE:
            pairs.update(combinations(paths, 2))
    rows = [{"a": uid("file", project, a), "b": uid("file", project, b), "n": n}
            for (a, b), n in pairs.items() if n >= 2]
    run("MATCH (:File {project: $p})-[r:CO_CHANGED]-() DELETE r", p=project)
    write_batch("""UNWIND $rows AS row
        MATCH (a:File {uid: row.a}), (b:File {uid: row.b})
        MERGE (a)-[r:CO_CHANGED]->(b) SET r.count = row.n""", rows)
    return len(rows)


def ingest_git(project: str, repo_dir: Path, log=print) -> list[dict]:
    commits = read_commits(repo_dir)
    log(f"git: {len(commits)} commits")
    write_commits(project, commits)
    n = write_cochange(project, commits)
    log(f"git: {n} co-change pairs (>= 2 shared commits)")
    return commits
