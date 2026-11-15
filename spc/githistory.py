"""Source A — local git mining: hotspots, bug classes, test co-change. Pure stats, offline."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from spc.diff import run_git

# Vendored / lockfile / generated noise excluded from hotspots and co-change stats.
EXCLUDED_PATH_PATTERNS = re.compile(
    r"(^|/)("
    r"vendor/|vendors/|node_modules/|\.venv/|venv/|dist/|build/|target/"
    r"|__pycache__/|\.next/|\.turbo/|coverage/|\.min\.js$|\.min\.css$"
    r")",
    re.IGNORECASE,
)
LOCKFILE_NAMES = {
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "Cargo.lock",
    "poetry.lock", "uv.lock", "Pipfile.lock", "composer.lock", "Gemfile.lock",
}

BUG_KEYWORDS = ("fix", "bugfix", "hotfix", "revert", "bug")
COMMIT_SUBJECT_SPLIT = re.compile(r"[\s:;\-–—()]+")

TOP_HOTSPOT_COUNT = 20


@dataclass
class GitEvidence:
    """Raw, JSON-serializable statistics mined from ``git log``."""

    commits_scanned: int = 0
    hotspots: list[tuple[str, int]] = field(default_factory=list)
    bug_classes: list[dict[str, object]] = field(default_factory=list)
    test_dirs: list[str] = field(default_factory=list)
    test_co_change_ratio: float = 0.0
    language_proportions: dict[str, float] = field(default_factory=dict)
    total_files_tracked: int = 0


def mine_git_history(repo_root: Path, max_commits: int) -> GitEvidence | None:
    """Mine the repo's history into :class:`GitEvidence`. None when git fails / empty repo."""
    # %x00 record separator makes subject/files unambiguous to parse.
    log = run_git(
        repo_root, "log", f"--max-count={max_commits}",
        "--name-only", "--format=%x00%s", "--",
    )
    if log is None:
        return None

    commits = _parse_name_only_log(log)
    if not commits:
        return None

    tracked = run_git(repo_root, "ls-files")
    all_files = tracked.splitlines() if tracked else []

    evidence = GitEvidence(commits_scanned=len(commits))
    evidence.hotspots = _hotspots(commits)
    evidence.bug_classes = _bug_classes(commits)
    evidence.test_dirs, evidence.test_co_change_ratio = _test_layout(commits)
    evidence.language_proportions = _languages(all_files or [f for c in commits for f in c.files])
    evidence.total_files_tracked = len(all_files)
    return evidence


@dataclass
class Commit:
    """One commit: subject line plus the files it touched."""

    subject: str
    files: tuple[str, ...]


def _parse_name_only_log(log: str) -> list[Commit]:
    """Parse ``git log --format=%x00%s --name-only`` output into commits.

    Records are NUL-prefixed: ``\\0subject\\n\\nfile1\\nfile2\\n…``.
    """
    commits: list[Commit] = []
    subject: str | None = None
    files: list[str] = []
    past_blank = False  # seen the blank line separating subject from file list

    def flush() -> None:
        nonlocal subject, files, past_blank
        if subject is not None:
            commits.append(Commit(subject=subject, files=tuple(files)))
        subject, files, past_blank = None, [], False

    for line in log.split("\n"):
        if line.startswith("\x00"):
            flush()
            subject = line[1:]
            continue
        if subject is None:
            continue  # nothing before the first record
        if line == "" and not past_blank:
            past_blank = True
            continue
        if line.strip():
            files.append(line)
    flush()
    return commits


def _is_noise(path: str) -> bool:
    """True for vendored / lockfile / generated paths excluded from stats."""
    name = path.rsplit("/", 1)[-1]
    return bool(EXCLUDED_PATH_PATTERNS.search(path)) or name in LOCKFILE_NAMES


def _hotspots(commits: list[Commit]) -> list[tuple[str, int]]:
    """Top-20 most-changed files AND directories (one level), noise-excluded."""
    file_counts: Counter[str] = Counter()
    dir_counts: Counter[str] = Counter()
    for commit in commits:
        for path in commit.files:
            if _is_noise(path):
                continue
            file_counts[path] += 1
            top_dir = path.split("/")[0]
            if "/" in path:
                dir_counts[top_dir + "/"] += 1
    combined: Counter[str] = Counter()
    combined.update(file_counts)
    combined.update(dir_counts)
    return combined.most_common(TOP_HOTSPOT_COUNT)


def _bug_classes(commits: list[Commit]) -> list[dict[str, object]]:
    """Frequency + top file clusters of fix/hotfix/revert commits, noise-excluded."""
    bug_files: Counter[str] = Counter()
    keyword_counts: Counter[str] = Counter()
    for commit in commits:
        words = {word.lower() for word in COMMIT_SUBJECT_SPLIT.split(commit.subject)}
        matched = [keyword for keyword in BUG_KEYWORDS if keyword in words]
        if not matched:
            continue
        for keyword in matched:
            keyword_counts[keyword] += 1
        for path in commit.files:
            if not _is_noise(path):
                bug_files[path] += 1
    return [
        {
            "keyword": keyword,
            "count": keyword_counts[keyword],
            "top_files": [path for path, _ in bug_files.most_common(5)],
        }
        for keyword in ("fix", "hotfix", "revert")
        if keyword_counts[keyword]
    ]


def _is_test_path(path: str) -> bool:
    """Heuristic: does this path look like a test file/dir?"""
    lowered = path.lower()
    return (
        "test" in Path(path).name.lower()
        or lowered.startswith("tests/")
        or lowered.startswith("test/")
        or "/tests/" in lowered
        or "/test/" in lowered
        or lowered.startswith("spec/")
        or "/spec/" in lowered
    )


def _test_layout(commits: list[Commit]) -> tuple[list[str], float]:
    """(test dirs, source↔test co-change ratio).

    Co-change ratio: of commits touching source files, the fraction that also
    touched at least one test file.
    """
    test_dir_counts: Counter[str] = Counter()
    source_commits = 0
    source_and_test_commits = 0
    for commit in commits:
        test_paths = [p for p in commit.files if _is_test_path(p)]
        source_paths = [p for p in commit.files if not _is_test_path(p) and not _is_noise(p)]
        for path in test_paths:
            top = path.split("/")[0]
            test_dir_counts[top + "/" if "/" in path else "(root)"] += 1
        if source_paths:
            source_commits += 1
            if test_paths:
                source_and_test_commits += 1
    ratio = (
        round(source_and_test_commits / source_commits, 3)
        if source_commits
        else 0.0
    )
    dirs = [d for d, _ in test_dir_counts.most_common(5)]
    return dirs, ratio


def _languages(all_files: list[str]) -> dict[str, float]:
    """Rough language proportions across tracked files."""
    from spc.languages import language_proportions

    return language_proportions([f for f in all_files if f and not _is_noise(f)])
