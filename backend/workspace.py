"""File tree, read/write files inside the repo, git diff/commit."""
from pathlib import Path

from fastapi import HTTPException
from git import Repo

from ingest.code_ingest import list_files


def safe_path(repo_dir: Path, rel: str) -> Path:
    p = (repo_dir / rel).resolve()
    if not p.is_relative_to(repo_dir.resolve()) or ".git" in p.relative_to(repo_dir.resolve()).parts:
        raise HTTPException(400, "Path outside repo")
    return p


def file_tree(repo_dir: Path) -> list[str]:
    return list_files(repo_dir)


def read_file(repo_dir: Path, rel: str) -> str:
    p = safe_path(repo_dir, rel)
    if not p.is_file():
        raise HTTPException(404, "No such file")
    return p.read_text("utf8", "replace")


def write_file(repo_dir: Path, rel: str, content: str):
    p = safe_path(repo_dir, rel)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, "utf8", newline="")


def changed_files(repo_dir: Path) -> list[str]:
    out = Repo(repo_dir).git.status("--porcelain", "-uall")
    return [line[3:].strip().strip('"') for line in out.splitlines() if line.strip()]


def diff(repo_dir: Path) -> str:
    repo = Repo(repo_dir)
    text = repo.git.diff("HEAD")
    for f in repo.untracked_files:
        text += f"\n--- /dev/null\n+++ b/{f}\n(new file)\n"
    return text


def commit(repo_dir: Path, message: str) -> str:
    repo = Repo(repo_dir)
    repo.git.add("-A")
    repo.git.commit("-m", message)
    return repo.head.commit.hexsha
