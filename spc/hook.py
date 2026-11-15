"""Install/remove the git pre-push hook. Idempotent via a marker comment."""

from __future__ import annotations

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
    """Absolute path of the pre-push hook inside *repo_root*."""
    return repo_root / HOOK_RELATIVE_PATH


def install_hook(repo_root: Path) -> str:
    """Append the marked hook block to ``.git/hooks/pre-push`` (idempotent).

    Returns one of: "installed", "already", "replaced" (an existing
    spiritcuffs-managed hook was refreshed).
    """
    path = hook_path(repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text(encoding="utf-8") if path.exists() else ""

    if MARKER_BEGIN in existing:
        # Refresh our block in place (keeps any user content around it).
        before = existing.split(MARKER_BEGIN)[0]
        after = existing.split(MARKER_END, 1)[1]
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
    path = hook_path(repo_root)
    if not path.exists() or MARKER_BEGIN not in path.read_text(encoding="utf-8"):
        return False
    text = path.read_text(encoding="utf-8")
    before = text.split(MARKER_BEGIN)[0]
    after = text.split(MARKER_END, 1)[1]
    if not _has_executable_lines(before + after):
        path.unlink()
        return True
    path.write_text(before + after, encoding="utf-8")
    return True


def _make_executable(path: Path) -> None:
    """chmod 755 — hooks must be executable; harmless if already so."""
    path.chmod(path.stat().st_mode | 0o755)
