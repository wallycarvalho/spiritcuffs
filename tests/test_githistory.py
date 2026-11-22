"""githistory mining tests against a real fixture repo."""

from __future__ import annotations

from spc.githistory import mine_git_history


def test_mine_hotspots_bug_classes_and_co_change(git_repo) -> None:
    repo = git_repo(
        {"src/auth.py": "def login(): ...\n", "src/api.py": "def handle(): ...\n"},
        later_commits=[
            ("fix: injection in auth", {"src/auth.py": "def login(): return 1\n"}),
            ("fix: crash in api", {"src/api.py": "def handle(): return 2\n"}),
            ("hotfix: urgent auth patch", {"src/auth.py": "def login(): return 3\n"}),
            ("add tests for auth", {"tests/test_auth.py": "def test_login(): ...\n"}),
            ("refactor api module", {"src/api.py": "def handle(): return 4\n"}),
            ("fix: revert bad api change", {"src/api.py": "def handle(): return 5\n"}),
            ("tests for api", {"tests/test_api.py": "def test_handle(): ...\n"}),
            ("fix: auth again", {"src/auth.py": "def login(): return 6\n"}),
            ("fix: auth tests too", {"src/auth.py": "def login(): return 7\n",
                                     "tests/test_auth.py": "def test_login(): return 7\n"}),
        ],
    )
    evidence = mine_git_history(repo, max_commits=500)
    assert evidence is not None
    assert evidence.commits_scanned == 10

    # Hotspots: src/auth.py changed in 4 commits (initial + 3 fixes) -> top file.
    hotspot_names = [name for name, _count in evidence.hotspots]
    assert "src/auth.py" in hotspot_names
    auth_count = dict(evidence.hotspots)["src/auth.py"]
    assert auth_count >= 4

    # Bug classes: "fix: revert bad api change" counts for both fix and revert;
    # fix appears in 5 subjects.
    by_keyword = {entry["keyword"]: entry for entry in evidence.bug_classes}
    assert by_keyword["fix"]["count"] == 5
    assert by_keyword["hotfix"]["count"] == 1
    assert by_keyword["revert"]["count"] == 1
    assert "src/auth.py" in by_keyword["fix"]["top_files"]

    # Test co-change: 2 of the source commits also touched tests.
    assert evidence.test_co_change_ratio > 0
    assert any("tests/" in d for d in evidence.test_dirs)


def test_bug_class_file_clusters_are_per_keyword(git_repo) -> None:
    """One hotfix must not inherit the file clusters of hundreds of fixes.

    ``top_files`` was accumulated in a single Counter shared by every keyword,
    so ``hotfix (count 1)`` reported the same files as ``fix (count 5)`` and the
    synthesis prompt presented one commit's files as a trend.
    """
    repo = git_repo(
        {"src/auth.py": "def login(): ...\n", "deploy/ops.py": "x = 1\n"},
        later_commits=[
            ("fix: injection in auth", {"src/auth.py": "def login(): return 1\n"}),
            ("fix: auth guard", {"src/auth.py": "def login(): return 2\n"}),
            ("fix: auth token", {"src/auth.py": "def login(): return 3\n"}),
            ("hotfix: urgent deploy patch", {"deploy/ops.py": "x = 2\n"}),
        ],
    )
    evidence = mine_git_history(repo, max_commits=100)
    assert evidence is not None
    by_keyword = {entry["keyword"]: entry for entry in evidence.bug_classes}

    assert by_keyword["fix"]["top_files"][0] == "src/auth.py"
    assert by_keyword["hotfix"]["top_files"] == ["deploy/ops.py"]


def test_mine_excludes_lockfiles_and_vendored(git_repo) -> None:
    repo = git_repo(
        {"src/a.py": "a = 1\n", "package-lock.json": "{}\n", "vendor/lib.py": "x\n"},
        later_commits=[
            ("chore deps", {"package-lock.json": '{"lock": 1}\n'}),
            ("vendor bump", {"vendor/lib.py": "y\n"}),
            ("real change", {"src/a.py": "a = 2\n"}),
        ],
    )
    evidence = mine_git_history(repo, max_commits=100)
    assert evidence is not None
    hotspot_names = [name for name, _ in evidence.hotspots]
    assert all("package-lock.json" not in name for name in hotspot_names)
    assert all(not name.startswith("vendor/") for name in hotspot_names)


def test_mine_languages_proportions(git_repo) -> None:
    repo = git_repo(
        {
            "src/a.py": "a=1\n",
            "src/b.py": "b=1\n",
            "src/c.ts": "const c=1;\n",
            "README.md": "# doc\n",
        },
    )
    evidence = mine_git_history(repo, max_commits=10)
    assert evidence is not None
    proportions = evidence.language_proportions
    assert proportions.get("python", 0) > proportions.get("typescript", 0)
    assert sum(proportions.values()) > 90  # rounding leaves slack


def test_mine_returns_none_outside_repo(tmp_path) -> None:
    assert mine_git_history(tmp_path, max_commits=10) is None


def test_mine_returns_none_on_empty_repo(git_repo) -> None:
    # A repo whose only commit has no files is degenerate; mining degrades to None.
    repo = git_repo(files={"README.md": "# only\n"})
    evidence = mine_git_history(repo, max_commits=10)
    assert evidence is not None  # 1 commit with a file is fine
    assert evidence.commits_scanned == 1
