"""Shared fixtures: a real temp git repo builder and env isolation."""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

GitFactory = Callable[[dict[str, str], list[tuple[str, str]]], Path]


def run_git(repo: Path, *args: str) -> None:
    """Run git in *repo*, raising on failure (fixture setup wants loud errors)."""
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def run_git_capture(repo: Path, *args: str) -> str:
    """Run git in *repo* and return stdout; raises on failure like run_git."""
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout


@pytest.fixture()
def git_repo(tmp_path: Path) -> Callable[..., Path]:
    """Factory: create a git repo with a first commit, then optional extra commits.

    Usage:
        repo = git_repo()                       # empty repo, one initial commit
        repo = git_repo({"src/a.py": "x=1"})    # initial commit with files
        git_repo(files, later_commits)          # + [(subject, {path: content}), ...]
    """
    counter = {"n": 0}

    def make(
        files: dict[str, str] | None = None,
        later_commits: list[tuple[str, dict[str, str]]] | None = None,
        branch: str = "main",
    ) -> Path:
        counter["n"] += 1
        repo = tmp_path / f"repo{counter['n']}"
        repo.mkdir()
        run_git(repo, "init", "-b", branch)
        run_git(repo, "config", "user.email", "test@example.com")
        run_git(repo, "config", "user.name", "Test")
        run_git(repo, "config", "commit.gpgsign", "false")
        _commit_files(repo, "initial commit", files or {"README.md": "# repo\n"})
        for subject, commit_files in later_commits or []:
            _commit_files(repo, subject, commit_files)
        return repo

    return make


def _commit_files(repo: Path, subject: str, files: dict[str, str]) -> None:
    """Stage and commit *files* in *repo* with *subject*."""
    for path, content in files.items():
        full = repo / path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content, encoding="utf-8")
        run_git(repo, "add", path)
    run_git(repo, "commit", "-m", subject)


@pytest.fixture(autouse=True)
def isolate_history_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Point SPC_HISTORY_DIR at a temp dir so tests never touch ~/.spc."""
    monkeypatch.setenv("SPC_HISTORY_DIR", str(tmp_path / "history"))
