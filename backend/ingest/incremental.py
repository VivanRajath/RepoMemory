"""Re-ingest only what a new commit touched."""
from pathlib import Path

from graph import run, uid
from ingest.code_ingest import derive_component_deps, list_files, parse_file, write_files
from ingest.deps_ingest import is_manifest, write_bumps, write_current_deps
from ingest.git_ingest import read_commits, write_cochange, write_commits


def ingest_commit(project: str, repo_dir: Path, sha: str, log=print) -> dict:
    commit = read_commits(repo_dir, rev=sha, max_count=1)[0]
    write_commits(project, [commit])
    changed = [f["path"] for f in commit["files"]]
    all_files = list_files(repo_dir)
    present = set(all_files)

    parsed = [parse_file(repo_dir, p, present) for p in changed if p in present]
    write_files(project, parsed)
    gone = [uid("file", project, p) for p in changed if p not in present]
    run("UNWIND $u AS x MATCH (f:File {uid: x}) SET f.deleted = true", u=gone)
    derive_component_deps(project)
    write_cochange(project, read_commits(repo_dir))  # cheap: one git log call
    manifests = [p for p in changed if is_manifest(p)]
    if manifests:
        write_current_deps(project, repo_dir, [p for p in all_files if is_manifest(p)])
        write_bumps(project, repo_dir, [commit])
    run("""MATCH (p:Project {uid: $pid}) OPTIONAL MATCH (f:File {project: $p}) WHERE f.language IS NOT NULL
           AND NOT coalesce(f.deleted, false) WITH p, sum(f.chars) AS c SET p.source_chars = c""",
        pid=uid("project", project, project), p=project)
    log(f"incremental: commit {sha[:7]} re-ingested {len(parsed)} files")
    return {"commit": commit, "commit_uid": uid("commit", project, sha),
            "file_uids": [uid("file", project, p) for p in changed]}
