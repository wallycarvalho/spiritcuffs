"""Git plumbing: upstream/staged/push diffs, diff hashing, repo discovery. All subprocess-based."""

from __future__ import annotations

import hashlib
import subprocess
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class DiffResult:
    """The reviewable diff, where it came from, and which files it touches."""

    text: str
    source: str  # "upstream" | "staged" | "push"
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


ZERO_SHA = "0" * 40


@dataclass(frozen=True)
class PushRef:
    """One pre-push stdin line: ``<local_ref> <local_sha> <remote_ref> <remote_sha>``."""

    local_ref: str
    local_sha: str
    remote_ref: str
    remote_sha: str


def parse_push_lines(stdin_text: str) -> list[PushRef]:
    """Parse pre-push hook stdin into :class:`PushRef` entries.

    Malformed lines (wrong field count, non-hex SHAs) are skipped — git
    controls this input, but fail-soft per line anyway.

    Assumes 40-char SHA-1 hashes (the pre-push stdin contract). SHA-256
    repos (64-char) have every line skipped → review silently skipped —
    acceptable fail-soft, not a crash.
    """
    refs: list[PushRef] = []
    for line in stdin_text.splitlines():
        parts = line.split()
        if len(parts) != 4:
            continue
        shas = (parts[1], parts[3])  # fields 0 and 2 are ref names, not SHAs
        if not all(
            len(sha) == 40 and all(c in "0123456789abcdef" for c in sha.lower())
            for sha in shas
        ):
            continue
        refs.append(PushRef(*parts))
    return refs


def push_diff(repo_root: Path, refs: list[PushRef]) -> DiffResult | None:
    """The diff of what is actually being pushed, from pre-push stdin refs.

    For each ref: ``git diff remote_sha..local_sha`` (two-dot) is exactly the
    commits being pushed. Edge cases:

    - local_sha zeros → branch/tag deletion → nothing to review, skipped
    - remote_sha zeros → new remote ref → diff against the merge-base with
      the branch's configured remote default branch (unresolvable → skipped)
    - unresolvable SHAs → that ref is skipped, others still reviewed

    Returns None when nothing reviewable remains (all deletions, empty
    changes, or no parseable refs). Never raises.
    """
    sections: list[str] = []
    seen_hashes: set[str] = set()
    for ref in refs:
        if set(ref.local_sha) == {"0"}:
            continue  # deletion: nothing to review
        if set(ref.remote_sha) == {"0"}:
            base = _new_ref_base(repo_root, ref)
        else:
            base = ref.remote_sha
        if base is None:
            continue
        text = run_git(repo_root, "diff", f"{base}..{ref.local_sha}")
        if text is None or not text.strip():
            continue
        digest = diff_hash(text)
        if digest in seen_hashes:
            continue  # two refs pushing the same commits — review once
        seen_hashes.add(digest)
        sections.append(text)
    if not sections:
        return None
    text = "\n".join(sections)
    return DiffResult(text=text, source="push", files=changed_files(text))


def _new_ref_base(repo_root: Path, ref: PushRef) -> str | None:
    """Merge-base with the remote default branch for a brand-new remote ref.

    A full-tree diff would review the entire repo; the merge-base with the
    remote's default branch scopes the diff to this branch's commits. When
    even that is unresolvable (e.g. orphan branch with no shared history),
    None makes the ref skipped — a wrong huge diff is worse than no review.
    """
    # stdin refs are namespaced (refs/heads/x) — the remote is NOT the ref
    # prefix. Use the branch's configured push remote, then the repo's
    # default remote, then common defaults.
    remote = run_git(repo_root, "config", "--get", f"branch.{_ref_name(ref.local_ref)}.remote")
    remote = (remote or "").strip() or None
    candidates = [c for c in (remote, "origin", "upstream") if c]
    for name in dict.fromkeys(candidates):
        for spec in (f"{name}/HEAD", f"{name}/main", f"{name}/master"):
            # --all lists every merge base on criss-cross merges; picking the
            # first is arbitrary and can miss branch commits in that rare
            # case — acceptable fail-soft.
            base = run_git(repo_root, "merge-base", "--all", spec, ref.local_sha)
            if base and base.strip():
                return base.strip().splitlines()[0]
    return None


def _ref_name(ref: str) -> str:
    """refs/heads/main → main; refs/heads/feature/sub → feature/sub.

    The whole remainder after ``refs/heads/`` — branch names may contain
    slashes, and the last path segment would look up the wrong git config
    key (``branch.feature/sub.remote`` is the real key).
    """
    prefix = "refs/heads/"
    if ref.startswith(prefix):
        return ref[len(prefix):]
    return ref.split("/")[-1]


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
    """Extract the touched file paths from a unified diff, in order of appearance.

    Prefers the ``--- a/<path>`` / ``+++ b/<path>`` lines: with spaces in paths
    (e.g. ``dir b/x``) the ``diff --git`` header itself is ambiguous, while the
    ---/+++ lines carry exactly one side each. Falls back to header parsing for
    sections without ---/+++ lines (binary files, header-only input from
    ``_split_into_file_sections``).
    """
    files: list[str] = []
    old_path: str | None = None
    saw_path_lines = False
    for line in diff_text.splitlines():
        if line.startswith("--- "):
            old_path = _parse_diff_path(line[len("--- "):])
        elif line.startswith("+++ "):
            saw_path_lines = True
            new_path = _parse_diff_path(line[len("+++ "):])
            if new_path == "/dev/null" and old_path is not None:
                files.append(old_path)  # deletion: only the --- side has a path
            else:
                files.append(new_path)
            old_path = None
        elif line.startswith("diff --git "):
            old_path = None  # a malformed section carries no paths over
    if saw_path_lines:
        return files
    return _files_from_headers(diff_text)


def _files_from_headers(diff_text: str) -> list[str]:
    """Fallback: parse ``diff --git a/<old> b/<new>`` headers (binary sections)."""
    files: list[str] = []
    for line in diff_text.splitlines():
        if not line.startswith("diff --git "):
            continue
        remainder = line[len("diff --git "):]
        parts = remainder.split(" b/")
        if len(parts) < 2:
            continue
        path = "b/" + " b/".join(parts[1:])
        files.append(path[2:].strip('"'))
    return files


def _parse_diff_path(raw: str) -> str:
    """Parse a ---/+++ path token: strip trailing tab, C-style quotes, a//b/ prefix."""
    token = raw.split("\t", 1)[0]
    if token == "/dev/null":
        return token
    if token.startswith('"') and token.endswith('"') and len(token) >= 2:
        token = _unescape_c_style(token[1:-1])
    if token[:2] in ("a/", "b/"):
        return token[2:]
    return token


def _unescape_c_style(text: str) -> str:
    """Decode git's C-style escapes (\\n, \\t, \\\\, \\NNN octal) in quoted paths.

    Consecutive octal escapes form UTF-8 bytes (é = ``\\303\\251``) and must be
    decoded as a group, not one character each.
    """
    escapes = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\"}
    decoded: list[str] = []
    pending_bytes = bytearray()
    index = 0

    def flush_bytes() -> None:
        if pending_bytes:
            decoded.append(pending_bytes.decode("utf-8", errors="replace"))
            pending_bytes.clear()

    while index < len(text):
        char = text[index]
        if char == "\\" and index + 1 < len(text):
            nxt = text[index + 1]
            if nxt in escapes:
                flush_bytes()
                decoded.append(escapes[nxt])
                index += 2
                continue
            if nxt.isdigit():  # octal byte escape, e.g. \303\251 for é
                octal = text[index + 1:index + 4]
                pending_bytes.append(int(octal, 8))
                index += 1 + len(octal)
                continue
        flush_bytes()
        decoded.append(char)
        index += 1
    flush_bytes()
    return "".join(decoded)


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
