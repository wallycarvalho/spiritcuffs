"""Hook install/remove idempotency, truncated markers, and core.hooksPath."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from conftest import run_git

from spc.hook import (
    HOOK_RELATIVE_PATH,
    MARKER_BEGIN,
    MARKER_END,
    _hook_target,
    install_hook,
    remove_hook,
)


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


def test_install_handles_truncated_marker_block(git_repo, capsys) -> None:
    """BEGIN without END (half-deleted block) must not crash install or remove."""
    repo = git_repo()
    hook_file = repo / HOOK_RELATIVE_PATH
    hook_file.parent.mkdir(parents=True, exist_ok=True)
    hook_file.write_text(
        "#!/bin/sh\necho user-thing\n" + MARKER_BEGIN + "\ndangling content",
        encoding="utf-8",
    )
    hook_file.chmod(0o755)

    assert install_hook(repo) == "replaced"
    text = _hook_text(repo)
    assert "user-thing" in text  # foreign content survives
    assert text.count(MARKER_BEGIN) == 1
    assert MARKER_END in text  # block is whole again
    assert "dangling content" not in text  # dead fragment dropped
    assert "truncated" in capsys.readouterr().err.lower()


def test_remove_handles_truncated_marker_block(git_repo) -> None:
    repo = git_repo()
    hook_file = repo / HOOK_RELATIVE_PATH
    hook_file.parent.mkdir(parents=True, exist_ok=True)
    hook_file.write_text("#!/bin/sh\n" + MARKER_BEGIN + "\nno end marker", encoding="utf-8")
    hook_file.chmod(0o755)

    assert remove_hook(repo) is True
    assert not hook_file.exists()  # nothing executable remained


def test_install_honors_core_hookspath(git_repo) -> None:
    """With core.hooksPath set, the hook must go where git actually looks."""
    repo = git_repo()
    custom = repo / ".githooks"
    run_git(repo, "config", "core.hooksPath", ".githooks")

    assert install_hook(repo) == "installed"
    assert (custom / "pre-push").is_file()
    assert MARKER_BEGIN in (custom / "pre-push").read_text(encoding="utf-8")
    mode = (custom / "pre-push").stat().st_mode
    assert mode & 0o111

    # The default location must NOT have been touched.
    assert not (repo / HOOK_RELATIVE_PATH).exists()

    assert remove_hook(repo) is True
    assert not (custom / "pre-push").exists()


def test_hook_target_resolves_absolute_hookspath(git_repo) -> None:
    repo = git_repo()
    run_git(repo, "config", "core.hooksPath", str(repo / "absolute-hooks"))
    assert _hook_target(repo) == repo / "absolute-hooks" / "pre-push"


def test_hook_target_falls_back_to_git_hooks(git_repo) -> None:
    repo = git_repo()  # fixture repos set no core.hooksPath
    assert _hook_target(repo) == repo / ".git" / "hooks" / "pre-push"


def test_install_into_hookspath_dir_runs_on_push(git_repo, monkeypatch) -> None:
    """End-to-end: git must actually execute a hook installed into core.hooksPath."""
    repo = git_repo()
    run_git(repo, "config", "core.hooksPath", ".githooks")
    install_hook(repo)

    # A real push needs a real (local bare) remote, or git fails before hooks run.
    bare = repo.parent / "bare-remote.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(bare)],
        check=True,
        capture_output=True,
    )
    run_git(repo, "remote", "add", "origin", str(bare))
    run_git(repo, "push", "-u", "origin", "main")  # establishes upstream

    # Pre-push only fires when a ref update is pending ("up to date" skips it),
    # so make one more commit before the dry-run push.
    (repo / "src").mkdir(exist_ok=True)
    (repo / "src/a.py").write_text("a=3\n", encoding="utf-8")
    run_git(repo, "add", "src/a.py")
    run_git(repo, "commit", "-m", "second commit")

    # Now a push (dry-run) reaches the pre-push stage. Under `uv run` the
    # project .venv/bin (which has `spc`) is on PATH and the hook would run a
    # real review whose rich output goes to stdout — which git swallows when
    # the hook exits 0. Strip the venv so the deterministic "not on PATH"
    # warning branch (stderr) is what executes.
    assert shutil.which("git") is not None
    git_bin = Path(shutil.which("git")).parent
    monkeypatch.setenv("PATH", f"{git_bin}:/usr/bin:/bin")
    result = subprocess.run(
        ["git", "-C", str(repo), "push", "--dry-run", "origin"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert "spiritcuffs" in result.stderr
