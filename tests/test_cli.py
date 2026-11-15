"""CLI command-level tests via typer's CliRunner (LLM mocked; no network)."""

from __future__ import annotations

import json

from rich.console import Console
from typer.testing import CliRunner

from spc import cli
from spc.config import CONFIG_FILENAME, load_config

runner = CliRunner()


def _patch_synth(monkeypatch, text: str = "# synthesized profile") -> None:
    """Patch the profile synthesis LLM call (spc.profile.llm.complete)."""
    import spc.profile as profile_mod

    monkeypatch.setattr(profile_mod.llm, "complete", lambda *a, **kw: text)


def _patch_review(monkeypatch, payload: str = "[]") -> dict:
    """Patch the review LLM call; counts invocations."""
    calls = {"n": 0}

    def fake(*args, **kwargs):  # noqa: ANN002, ANN003
        calls["n"] += 1
        return payload

    monkeypatch.setattr(cli.review_mod.llm, "complete", fake)
    return calls


def test_init_writes_config_hook_gitignore_profile_and_reviews(
    git_repo, monkeypatch, tmp_path
) -> None:
    repo = git_repo({"src/a.py": "a=1\n"})
    monkeypatch.chdir(repo)
    monkeypatch.setattr(cli, "console", Console(file=open("/dev/null", "w")))  # noqa: SIM115
    _patch_synth(monkeypatch)
    review_calls = _patch_review(monkeypatch)

    result = runner.invoke(cli.app, ["init"])
    assert result.exit_code == 0, result.output

    assert (repo / CONFIG_FILENAME).is_file()
    assert load_config(repo).learn.learn_max_commits == 2000  # defaults landed
    assert (repo / ".git" / "hooks" / "pre-push").is_file()
    # No .gitignore in fixture repo -> init must fall back to .git/info/exclude.
    assert ".spc/" in (repo / ".git" / "info" / "exclude").read_text(encoding="utf-8")
    assert (repo / ".spc" / "profile.md").is_file()
    assert (repo / ".spc" / "evidence.json").is_file()
    assert review_calls["n"] == 1  # first review ran immediately


def test_init_is_rerunnable(git_repo, monkeypatch) -> None:
    repo = git_repo({"src/a.py": "a=1\n"})
    monkeypatch.chdir(repo)
    null = Console(file=open("/dev/null", "w"))  # noqa: SIM115
    monkeypatch.setattr(cli, "console", null)
    _patch_synth(monkeypatch)
    _patch_review(monkeypatch)

    assert runner.invoke(cli.app, ["init"]).exit_code == 0
    config_before = (repo / CONFIG_FILENAME).read_text(encoding="utf-8")
    result = runner.invoke(cli.app, ["init"])
    assert result.exit_code == 0
    assert (repo / CONFIG_FILENAME).read_text(encoding="utf-8") == config_before


def test_hook_remove_uninstalls(git_repo, monkeypatch) -> None:
    repo = git_repo({"src/a.py": "a=1\n"})
    monkeypatch.chdir(repo)
    monkeypatch.setattr(cli, "console", Console(file=open("/dev/null", "w")))  # noqa: SIM115

    assert runner.invoke(cli.app, ["hook"]).exit_code == 0
    assert (repo / ".git" / "hooks" / "pre-push").is_file()
    assert runner.invoke(cli.app, ["hook", "--remove"]).exit_code == 0
    assert not (repo / ".git" / "hooks" / "pre-push").exists()
    # Removing again is a clean no-op:
    assert runner.invoke(cli.app, ["hook", "--remove"]).exit_code == 0


def test_history_lists_runs(git_repo, monkeypatch) -> None:
    from conftest import run_git

    repo = git_repo({"src/a.py": "a=1\n"})
    (repo / "src/a.py").write_text("a=2\n", encoding="utf-8")
    run_git(repo, "add", "src/a.py")
    monkeypatch.chdir(repo)
    null = Console(file=open("/dev/null", "w"))  # noqa: SIM115
    monkeypatch.setattr(cli, "console", null)
    _patch_synth(monkeypatch)
    _patch_review(
        monkeypatch,
        '[{"severity":"warn","file":"src/a.py","line":1,"message":"m","suggestion":""}]',
    )

    assert runner.invoke(cli.app, ["init"]).exit_code == 0

    from spc.log import history_file

    records = json.loads(
        history_file(repo).read_text(encoding="utf-8").strip().splitlines()[-1]
    )
    assert records["findings"][0]["severity"] == "warn"

    monkeypatch.setattr(cli, "console", Console())
    result = runner.invoke(cli.app, ["history"])
    assert result.exit_code == 0
    assert "SpiritCuffs review history" in result.output


def test_version_command() -> None:
    result = runner.invoke(cli.app, ["version"])
    assert result.exit_code == 0
    assert "spiritcuffs" in result.output


def test_review_outside_repo_exits_zero(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(cli.app, ["review"])
    assert result.exit_code == 0
    assert "not inside a git repository" in result.output


def test_gitignore_uses_info_exclude_when_no_gitignore(git_repo, monkeypatch) -> None:
    """A repo with no .gitignore gets .spc/ in .git/info/exclude instead."""
    repo = git_repo({"src/a.py": "a=1\n"})
    assert not (repo / ".gitignore").exists()  # fixture must not have one
    monkeypatch.chdir(repo)
    null = Console(file=open("/dev/null", "w"))  # noqa: SIM115
    monkeypatch.setattr(cli, "console", null)
    _patch_synth(monkeypatch)
    _patch_review(monkeypatch)

    assert runner.invoke(cli.app, ["init"]).exit_code == 0
    exclude = repo / ".git" / "info" / "exclude"
    assert ".spc/" in exclude.read_text(encoding="utf-8")
