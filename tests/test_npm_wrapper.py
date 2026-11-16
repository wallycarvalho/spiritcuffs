"""npm wrapper (npm/bin/cli.js): arg/exit-code forwarding, uv hint — via real node."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

NPM_CLI = Path(__file__).resolve().parent.parent / "npm" / "bin" / "cli.js"

node = shutil.which("node")
pytestmark = pytest.mark.skipif(node is None, reason="node not available")


def _fakebin(tmp_path: Path, *, uvx: bool) -> Path:
    """A PATH dir whose uvx (or nothing) probes successfully."""
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    if uvx:
        script = bin_dir / "uvx"
        script.write_text(
            "#!/bin/sh\n"
            'if [ "$1" = "--version" ]; then exit 0; fi\n'
            'echo "runner args: $@"\n'
            "exit 7\n",
            encoding="utf-8",
        )
        script.chmod(0o755)
    return bin_dir


def test_wrapper_forwards_args_and_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", f"{_fakebin(tmp_path, uvx=True)}:/usr/bin:/bin")
    result = subprocess.run(
        [node, str(NPM_CLI), "init", "--flag"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert "runner args: --from spiritcuffs spc init --flag" in result.stdout
    assert result.returncode == 7  # child's exit code forwarded


def test_wrapper_missing_runner_prints_uv_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("PATH", f"{empty}:/usr/bin:/bin")
    result = subprocess.run(
        [node, str(NPM_CLI)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 1
    assert "astral.sh/uv" in result.stderr
    assert "pipx" in result.stderr


def test_wrapper_falls_back_to_pipx(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = _fakebin(tmp_path, uvx=False)  # no uvx → pipx path
    pipx = bin_dir / "pipx"
    pipx.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then exit 0; fi\n'
        'echo "pipx args: $@"\n'
        "exit 5\n",
        encoding="utf-8",
    )
    pipx.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    result = subprocess.run(
        [node, str(NPM_CLI), "review"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert "pipx args: run --spec spiritcuffs spc review" in result.stdout
    assert result.returncode == 5
