"""Git plumbing: upstream/staged diffs, diff hashing, repo discovery. All subprocess-based."""

from __future__ import annotations

import hashlib
import subprocess
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class DiffResult:
    """The reviewable diff, where it came from, and which files it touches."""

    text: str
    source: str  # "upstream" | "staged"
    files: list[str]
    dropped_files: list[str] = field(default_factory=list)


def find_repo_root(start: Path | None = None) -> Path | None:
    """Walk up from *start* (default: cwd) to find the git repo root; None outside a repo."""
    directory = (start or Path.cwd()).resolve()
    for candidate in (directory, *directory.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def run_git(repo_root: Path, *args: str) -> str | None:
    """Run a git command in *repo_root*; return stdout, or None on any failure."""
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo_root), *args],
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return completed.stdout


def upstream_diff(repo_root: Path) -> str | None:
    """``git diff @{upstream}...HEAD`` — None when there is no upstream (or git fails)."""
    return run_git(repo_root, "diff", "@{upstream}...HEAD")


def staged_diff(repo_root: Path) -> str | None:
    """``git diff --cached`` — None when git fails (e.g. detached-HEAD edge cases)."""
    return run_git(repo_root, "diff", "--cached")


def collect_diff(repo_root: Path) -> DiffResult | None:
    """Get the reviewable diff: upstream first, staged fallback, None when both are empty.

    Returns None also when we're outside a repo or git itself is broken —
    callers treat None as "nothing to review / fail soft".
    """
    diff = upstream_diff(repo_root)
    source = "upstream"
    if diff is None:
        diff = staged_diff(repo_root)
        source = "staged"
    if diff is None or not diff.strip():
        return None
    return DiffResult(text=diff, source=source, files=changed_files(diff))


def changed_files(diff_text: str) -> list[str]:
    """Extract the touched file paths from a unified diff, in order of appearance."""
    files: list[str] = []
    for line in diff_text.splitlines():
        if not line.startswith("diff --git "):
            continue
        # Format: "diff --git a/<path> b/<path>" — paths may be quoted if they
        # contain special chars; take the b/ side and strip surrounding quotes.
        remainder = line[len("diff --git "):]
        parts = remainder.split(" b/")
        if len(parts) < 2:
            continue
        path = "b/" + " b/".join(parts[1:])
        path = path[2:]
        path = path.strip('"')
        files.append(path)
    return files


def diff_hash(diff_text: str) -> str:
    """sha256 hex digest of the diff text."""
    return hashlib.sha256(diff_text.encode("utf-8")).hexdigest()


def truncate_diff(diff_text: str, max_kb: int) -> tuple[str, list[str]]:
    """Truncate *diff_text* to *max_kb*, dropping whole file sections.

    Returns (truncated_diff, dropped_files). Never silently drops a file: the
    caller is expected to list *dropped_files* in the prompt.
    """
    diff_bytes = diff_text.encode("utf-8")
    if len(diff_bytes) <= max_kb * 1024:
        return diff_text, []

    sections = _split_into_file_sections(diff_text)
    kept: list[str] = []
    dropped: list[str] = []
    budget = max_kb * 1024
    for file_path, section_text in sections:
        if len(section_text.encode("utf-8")) <= budget:
            kept.append(section_text)
            budget -= len(section_text.encode("utf-8"))
        else:
            dropped.append(file_path)
    return "".join(kept), dropped


def _split_into_file_sections(diff_text: str) -> list[tuple[str, str]]:
    """Split a unified diff into (file_path, section_text) pairs, in order."""
    sections: list[tuple[str, str]] = []
    current_file: str | None = None
    current_lines: list[str] = []
    for line in diff_text.splitlines(keepends=True):
        if line.startswith("diff --git "):
            if current_file is not None and current_lines:
                sections.append((current_file, "".join(current_lines)))
            derived = changed_files(line)
            current_file = derived[0] if derived else "(unknown file)"
            current_lines = [line]
        elif current_file is not None:
            current_lines.append(line)
        # Lines before the first file header are dropped with the truncation.
    if current_file is not None and current_lines:
        sections.append((current_file, "".join(current_lines)))
    return sections


def pathhash8(repo_path: Path) -> str:
    """First 8 hex chars of sha256 over the repo's absolute path (collision-safe slug suffix)."""
    return hashlib.sha256(str(repo_path.resolve()).encode("utf-8")).hexdigest()[:8]
