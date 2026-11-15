"""Install/remove the git pre-push hook. Idempotent via a marker comment."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

HOOK_RELATIVE_PATH = Path(".git") / "hooks" / "pre-push"
MARKER_BEGIN = "# >>> spiritcuffs pre-push >>>"
MARKER_END = "# <<< spiritcuffs pre-push <<<"
HOOK_BLOCK = f"""{MARKER_BEGIN}
# SpiritCuffs: LLM review before push. Bypass: git push --no-verify
# Runs via sh (Git for Windows ships bash for hooks).
if command -v spc >/dev/null 2>&1; then
    spc review
else
    echo "spiritcuffs: 'spc' not on PATH — skipping review (install: pipx install spiritcuffs)" >&2
fi
{MARKER_END}
"""


def hook_path(repo_root: Path) -> Path:
    """Default pre-push hook location (``.git/hooks/pre-push``) inside *repo_root*."""
    return repo_root / HOOK_RELATIVE_PATH


def _hooks_dir(repo_root: Path) -> Path:
    """The directory git actually runs hooks from, honoring ``core.hooksPath``.

    Husky and similar managers set core.hooksPath to a repo-relative (or
    absolute) directory; installing into ``.git/hooks/`` there would be a
    hook git never executes.
    """
    try:
        configured = subprocess.run(
            ["git", "-C", str(repo_root), "config", "--get", "core.hooksPath"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        configured = ""
    if not configured:
        return hook_path(repo_root).parent
    hooks_dir = Path(configured)
    if not hooks_dir.is_absolute():
        hooks_dir = repo_root / hooks_dir
    return hooks_dir


def _hook_target(repo_root: Path) -> Path:
    """The pre-push file git will actually execute for *repo_root*."""
    return _hooks_dir(repo_root) / "pre-push"


def install_hook(repo_root: Path) -> str:
    """Append the marked hook block to the pre-push hook (idempotent).

    Returns one of: "installed", "already", "replaced" (an existing
    spiritcuffs-managed hook was refreshed).
    """
    path = _hook_target(repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text(encoding="utf-8") if path.exists() else ""

    if MARKER_BEGIN in existing:
        # Refresh our block in place (keeps any user content around it).
        before, after = _split_on_block(existing)
        path.write_text(before + HOOK_BLOCK + after, encoding="utf-8")
        return "replaced"

    if existing.strip():
        # A foreign hook exists — append ours; pre-push is a single script, so the
        # appended block simply runs after the existing logic.
        separator = "" if existing.endswith("\n") else "\n"
        path.write_text(existing + separator + HOOK_BLOCK, encoding="utf-8")
        _make_executable(path)
        return "installed"

    path.write_text("#!/bin/sh\n" + HOOK_BLOCK, encoding="utf-8")
    _make_executable(path)
    return "installed"


def _split_on_block(text: str) -> tuple[str, str]:
    """Split *text* around the marked block into (before, after).

    Tolerates a truncated block (BEGIN without END): the orphaned BEGIN and
    everything after it are treated as dead content — kept in *before* only up
    to the BEGIN line, rest dropped with a warning.
    """
    before = text.split(MARKER_BEGIN)[0]
    if MARKER_END in text:
        return before, text.split(MARKER_END, 1)[1]
    print(
        "spiritcuffs: found a truncated spiritcuffs hook block (missing end marker) "
        "— replacing it.",
        file=sys.stderr,
    )
    return before, ""


def _has_executable_lines(text: str) -> bool:
    """True when *text* contains any line a shell would actually run.

    Shebangs, comments and blank lines don't count — a hook consisting only
    of those is dead scaffolding and gets unlinked on removal.
    """
    return any(
        line.strip() and not line.strip().startswith("#") for line in text.splitlines()
    )


def remove_hook(repo_root: Path) -> bool:
    """Remove the marked spiritcuffs block from the hook. True if anything was removed.

    When nothing executable remains (only a shebang/comments), the hook file
    is unlinked entirely.
    """
    path = _hook_target(repo_root)
    if not path.exists() or MARKER_BEGIN not in path.read_text(encoding="utf-8"):
        return False
    before, after = _split_on_block(path.read_text(encoding="utf-8"))
    if not _has_executable_lines(before + after):
        path.unlink()
        return True
    path.write_text(before + after, encoding="utf-8")
    return True


def _make_executable(path: Path) -> None:
    """chmod 755 — hooks must be executable; harmless if already so."""
    path.chmod(path.stat().st_mode | 0o755)
