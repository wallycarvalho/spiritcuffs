"""Diff plumbing tests: fallbacks, changed-file extraction, truncation, hashing."""

from __future__ import annotations

from pathlib import Path

from conftest import run_git, run_git_capture

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


def test_changed_files_handles_path_containing_b_slash(git_repo) -> None:
    """Regression: 'dir b/x' must not be split on its ' b/' at the diff header."""
    repo = git_repo({"README.md": "# repo\n"})
    (repo / "dir b").mkdir()
    (repo / "dir b/x").write_text("one\n", encoding="utf-8")
    run_git(repo, "add", "dir b/x")
    run_git(repo, "commit", "-m", "add dir b/x")
    (repo / "dir b/x").write_text("two\n", encoding="utf-8")

    diff = run_git_capture(repo, "diff")
    assert "b/dir b/x" in diff  # the ambiguous header form git actually emits
    assert changed_files(diff) == ["dir b/x"]


def test_changed_files_handles_quoted_and_special_paths() -> None:
    """Quoted headers (quotes/non-ASCII) and /dev/null sides parse correctly."""
    diff = (
        'diff --git "a/we\\"ird" "b/we\\"ird"\n'
        '--- "a/we\\"ird"\n'
        '+++ "b/we\\"ird"\n'
        "@@ -1 +1 @@\n-a\n+b\n"
        'diff --git "a/h\\303\\251llo.txt" "b/h\\303\\251llo.txt"\n'
        '--- "a/h\\303\\251llo.txt"\n'
        '+++ "b/h\\303\\251llo.txt"\n'
        "@@ -1 +1 @@\n-a\n+b\n"
        "diff --git a/gone.txt b/gone.txt\n"
        "--- a/gone.txt\n+++ /dev/null\n"
        "@@ -1 +0,0 @@\n-gone\n"
    )
    assert changed_files(diff) == ['we"ird', "héllo.txt", "gone.txt"]


def test_changed_files_binary_section_uses_header_fallback() -> None:
    """Binary sections carry no ---/+++ lines; the header fallback names them."""
    diff = (
        "diff --git a/logo.png b/logo.png\n"
        "index 1234567..89abcde 100644\n"
        "Binary files a/logo.png and b/logo.png differ\n"
    )
    assert changed_files(diff) == ["logo.png"]


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
