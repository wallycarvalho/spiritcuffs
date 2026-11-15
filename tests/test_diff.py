"""Diff plumbing tests: fallbacks, changed-file extraction, truncation, hashing."""

from __future__ import annotations

from pathlib import Path

from conftest import run_git

from spc.diff import (
    changed_files,
    collect_diff,
    diff_hash,
    find_repo_root,
    pathhash8,
    truncate_diff,
)


def test_collect_diff_prefers_upstream(git_repo) -> None:
    repo = git_repo(
        {"src/a.py": "x = 1\n"},
        later_commits=[("add b", {"src/b.py": "y = 2\n"})],
    )
    # Create upstream tracking by cloning and pushing to a bare remote.
    remote = repo.parent / "remote.git"
    run_git_repo = ["init", "--bare", "-b", "main", str(remote)]
    run_git(repo.parent, *run_git_repo)
    run_git(repo, "remote", "add", "origin", str(remote))
    run_git(repo, "push", "-u", "origin", "main")
    # New local commit ahead of upstream -> upstream diff shows it.
    (repo / "src/c.py").write_text("z = 3\n", encoding="utf-8")
    run_git(repo, "add", ".")
    run_git(repo, "commit", "-m", "add c")

    result = collect_diff(repo)
    assert result is not None
    assert result.source == "upstream"
    assert "src/c.py" in result.files


def test_collect_diff_falls_back_to_staged(git_repo) -> None:
    repo = git_repo({"src/a.py": "x = 1\n"})
    (repo / "src/d.py").write_text("w = 4\n", encoding="utf-8")
    run_git(repo, "add", "src/d.py")

    result = collect_diff(repo)
    assert result is not None
    assert result.source == "staged"
    assert "src/d.py" in result.files


def test_collect_diff_returns_none_when_nothing_to_review(git_repo) -> None:
    repo = git_repo({"src/a.py": "x = 1\n"})
    assert collect_diff(repo) is None


def test_changed_files_parses_renames_and_order() -> None:
    diff = (
        "diff --git a/src/one.py b/src/one.py\n"
        "--- a/src/one.py\n+++ b/src/one.py\n@@ -1 +1 @@\n-x\n+y\n"
        "diff --git a/two.ts b/two.ts\n"
        "--- a/two.ts\n+++ b/two.ts\n@@ -1 +1 @@\n-a\n+b\n"
    )
    assert changed_files(diff) == ["src/one.py", "two.ts"]


def test_truncate_diff_lists_dropped_files() -> None:
    small = "diff --git a/small.py b/small.py\n@@ -1 +1 @@\n+x = 1\n"
    big_content = "+" + "y" * 3000 + "\n"
    big = f"diff --git a/big.py b/big.py\n@@ -1 +1 @@\n{big_content}"
    diff = small + big
    truncated, dropped = truncate_diff(diff, max_kb=1)
    assert "a/small.py" in truncated
    assert "big.py" not in truncated
    assert dropped == ["big.py"]


def test_truncate_diff_noop_under_limit() -> None:
    diff = "diff --git a/x.py b/x.py\n@@ -1 +1 @@\n+ok\n"
    truncated, dropped = truncate_diff(diff, max_kb=200)
    assert truncated == diff
    assert dropped == []


def test_diff_hash_stable_and_hex() -> None:
    assert diff_hash("abc") == diff_hash("abc")
    assert diff_hash("abc") != diff_hash("abd")
    assert len(diff_hash("abc")) == 64


def test_pathhash8_distinguishes_paths(tmp_path: Path) -> None:
    a = tmp_path / "repoA"
    b = tmp_path / "repoB"
    a.mkdir()
    b.mkdir()
    assert pathhash8(a) != pathhash8(b)
    assert len(pathhash8(a)) == 8


def test_find_repo_root_walks_up(git_repo) -> None:
    repo = git_repo()
    nested = repo / "src" / "deep"
    nested.mkdir(parents=True)
    assert find_repo_root(nested) == repo.resolve()
