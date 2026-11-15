"""Hook install/remove idempotency, including a pre-existing foreign hook."""

from __future__ import annotations

from pathlib import Path

from spc.hook import HOOK_RELATIVE_PATH, MARKER_BEGIN, install_hook, remove_hook


def _hook_text(repo: Path) -> str:
    return (repo / HOOK_RELATIVE_PATH).read_text(encoding="utf-8")


def test_install_creates_executable_hook(git_repo) -> None:
    repo = git_repo()
    assert install_hook(repo) == "installed"
    text = _hook_text(repo)
    assert text.startswith("#!/bin/sh")
    assert MARKER_BEGIN in text
    assert "spc review" in text
    mode = (repo / HOOK_RELATIVE_PATH).stat().st_mode
    assert mode & 0o111  # executable bit set


def test_install_is_idempotent(git_repo) -> None:
    repo = git_repo()
    first = install_hook(repo)
    second = install_hook(repo)
    assert first == "installed"
    assert second == "replaced"  # refreshes in place, no duplicate block
    text = _hook_text(repo)
    assert text.count(MARKER_BEGIN) == 1


def test_remove_removes_block_only(git_repo) -> None:
    repo = git_repo()
    install_hook(repo)
    assert remove_hook(repo) is True
    assert not (repo / HOOK_RELATIVE_PATH).exists()


def test_remove_keeps_foreign_hook_content(git_repo) -> None:
    repo = git_repo()
    hook_path = repo / HOOK_RELATIVE_PATH
    hook_path.parent.mkdir(parents=True, exist_ok=True)
    hook_path.write_text("#!/bin/sh\necho custom-check\n", encoding="utf-8")
    hook_path.chmod(0o755)

    install_hook(repo)  # appends after foreign content
    text = _hook_text(repo)
    assert "custom-check" in text
    assert MARKER_BEGIN in text

    assert remove_hook(repo) is True
    after = _hook_text(repo)
    assert "custom-check" in after
    assert MARKER_BEGIN not in after


def test_remove_without_hook_is_noop(git_repo) -> None:
    repo = git_repo()
    assert remove_hook(repo) is False
