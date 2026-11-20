"""Shared fixtures: a real temp git repo builder and env isolation."""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

GitFactory = Callable[[dict[str, str], list[tuple[str, str]]], Path]

# --- Secret fixtures -------------------------------------------------------
#
# PRIVACY / CI: these must never appear as literal strings in the repo.
# GitHub's push protection matches the AWS access-key-id shape and any PEM
# private-key header, so a literal fixture blocks every push to a protected
# repo — including ours, which is how this problem was found. Assembling the
# values at runtime keeps the scanner tests real while leaving nothing in the
# tree for a scanner (ours or GitHub's) to match.
#
# ``_AWS_KEY`` still exercises the real pattern: ``AKIA`` prefix, 16 uppercase
# alphanumerics, no leading/trailing junk.

_AWS_KEY_BODY = "0123456789" + "ABCDEF"
AWS_FIXTURE_KEY = "AKIA" + _AWS_KEY_BODY
PRIVATE_KEY_HEADER = "-----BEGIN " + "RSA PRIVATE KEY" + "-----"
PRIVATE_KEY_FOOTER = "-----END " + "RSA PRIVATE KEY" + "-----"


def private_key_block(body_lines: list[str]) -> str:
    """A structurally valid PEM block for tests, with no matchable header literal."""
    return "\n".join([PRIVATE_KEY_HEADER, *body_lines, PRIVATE_KEY_FOOTER])


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


@pytest.fixture(autouse=True)
def isolate_user_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Redirect the user config (~/.spc/config.toml) to a temp path in every test."""
    monkeypatch.setenv("SPC_USER_CONFIG", str(tmp_path / "usercfg" / "config.toml"))


@pytest.fixture(autouse=True)
def no_live_model_listing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the wizard's live model fetch to its fail-soft fallback in every test.

    The model-listing step makes a real HTTP call by design; without this stub
    any interactive wizard test would reach api.openrouter.ai etc. Tests that
    exercise the real fetch behavior re-enable it with
    ``enable_live_model_fetch`` — do NOT restore ``spc.setup._fetch_live_models``
    manually, the original is captured before any stubbing.
    """
    import spc.setup as setup_mod

    monkeypatch.setattr(
        setup_mod, "_fetch_live_models", lambda *args, **kwargs: None
    )


@pytest.fixture
def enable_live_model_fetch(monkeypatch: pytest.MonkeyPatch):
    """Re-enable the wizard's real _fetch_live_models, returning the module.

    Used by the TestLiveModelListing tests: they stub
    ``spc.setup.list_chat_models`` (the network boundary inside the fetch) and
    need the fetch itself live. Reads ``spc.setup._FETCH_LIVE_MODELS_REAL``
    (captured after the original ``_fetch_live_models`` definition). Yields the spc.setup module for asserting on
    patch targets.
    """
    import spc.setup as setup_mod

    real = setup_mod._FETCH_LIVE_MODELS_REAL
    monkeypatch.setattr(setup_mod, "_fetch_live_models", real)
    return setup_mod
