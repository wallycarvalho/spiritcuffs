"""End-to-end `spc review` flow via execute_review: secrets, strict mode, empty diffs."""

from __future__ import annotations

import pytest
from rich.console import Console

from spc import cli, review
from spc.config import SpcConfig
from spc.conventions import ConventionFile


@pytest.fixture()
def quiet_console() -> Console:
    return Console(file=open("/dev/null", "w"))  # noqa: SIM115 — tests only


def test_review_staged_diff_finds_issues(git_repo, monkeypatch) -> None:
    repo = git_repo({"src/a.py": "x = 1\n"})
    (repo / "src/a.py").write_text("import os\nos.system('rm -rf ' + user_input)\n", encoding="utf-8")
    from conftest import run_git

    run_git(repo, "add", ".")

    def fake_complete(*args, **kwargs):  # noqa: ANN002, ANN003
        return (
            '[{"severity":"critical","file":"src/a.py","line":2,'
            '"message":"shell injection","suggestion":"shlex.quote"}]'
        )

    monkeypatch.setattr(review.llm, "complete", fake_complete)
    code = cli.execute_review(repo, SpcConfig())
    assert code == 0  # not strict


def test_review_strict_blocks_on_critical(git_repo, monkeypatch, quiet_console) -> None:
    repo = git_repo({"src/a.py": "x = 1\n"})
    (repo / "src/a.py").write_text("x = 2\n", encoding="utf-8")
    from conftest import run_git

    run_git(repo, "add", ".")

    monkeypatch.setattr(
        review.llm,
        "complete",
        lambda *a, **kw: '[{"severity":"critical","file":"src/a.py","line":1,"message":"m","suggestion":"s"}]',
    )
    config = SpcConfig(review=SpcConfig().review.__class__(strict=True))
    assert cli.execute_review(repo, config) == 1


def test_review_strict_passes_on_warn_only(git_repo, monkeypatch) -> None:
    repo = git_repo({"src/a.py": "x = 1\n"})
    (repo / "src/a.py").write_text("x = 2\n", encoding="utf-8")
    from conftest import run_git

    run_git(repo, "add", ".")
    monkeypatch.setattr(
        review.llm,
        "complete",
        lambda *a, **kw: '[{"severity":"warn","file":"src/a.py","line":1,"message":"m","suggestion":""}]',
    )
    config = SpcConfig(review=SpcConfig().review.__class__(strict=True))
    assert cli.execute_review(repo, config) == 0


def test_review_empty_diff_is_silent_zero(git_repo) -> None:
    repo = git_repo({"src/a.py": "x = 1\n"})
    assert cli.execute_review(repo, SpcConfig()) == 0


def test_review_llm_failure_returns_zero(git_repo, monkeypatch) -> None:
    from spc.llm import LlmError

    repo = git_repo({"src/a.py": "x = 1\n"})
    (repo / "src/a.py").write_text("x = 2\n", encoding="utf-8")
    from conftest import run_git

    run_git(repo, "add", ".")

    def boom(*args, **kwargs):  # noqa: ANN002, ANN003
        raise LlmError("down")

    monkeypatch.setattr(review.llm, "complete", boom)
    assert cli.execute_review(repo, SpcConfig()) == 0


def test_review_blocked_on_secrets_skips_llm(git_repo, monkeypatch) -> None:
    repo = git_repo({"config.py": "x = 1\n"})
    (repo / "config.py").write_text('AWS_KEY = "AKIAIOSFODNN7EXAMPLE"\n', encoding="utf-8")
    from conftest import run_git

    run_git(repo, "add", ".")

    calls = {"n": 0}

    def fake_complete(*args, **kwargs):  # noqa: ANN002, ANN003
        calls["n"] += 1
        return "[]"

    monkeypatch.setattr(review.llm, "complete", fake_complete)
    config = SpcConfig(secrets=SpcConfig().secrets.__class__(block_on_secrets=True))
    assert cli.execute_review(repo, config) == 0
    assert calls["n"] == 0  # LLM never called — diff stayed local


def test_review_warns_but_continues_on_secrets_by_default(git_repo, monkeypatch) -> None:
    repo = git_repo({"config.py": "x = 1\n"})
    (repo / "config.py").write_text('AWS_KEY = "AKIAIOSFODNN7EXAMPLE"\n', encoding="utf-8")
    from conftest import run_git

    run_git(repo, "add", ".")
    monkeypatch.setattr(review.llm, "complete", lambda *a, **kw: "[]")
    # Default: warn loudly but still run the LLM and exit 0.
    assert cli.execute_review(repo, SpcConfig()) == 0


def test_build_prompt_includes_profile_conventions_hotspots_dropped() -> None:
    from spc.diff import DiffResult
    from spc.review import build_prompt

    diff = DiffResult(
        text="diff --git a/src/auth.py b/src/auth.py\n",
        source="upstream",
        files=["src/auth.py"],
        dropped_files=["huge/generated.js"],
    )
    prompt = build_prompt(
        diff,
        profile="# learned profile",
        conventions=[ConventionFile(name="CONTRIBUTING.md", content="Be kind.")],
        config=SpcConfig(),
        hotspot_paths={"src/auth.py"},
    )
    assert "# learned profile" in prompt
    assert "Be kind." in prompt
    assert "src/auth.py" in prompt
    assert "huge/generated.js" in prompt  # dropped files are LISTED
    assert "NOT reviewed" in prompt
    assert "Language-specific" in prompt


def test_hotspot_paths_from_evidence_files_only(tmp_path) -> None:
    import json

    from spc.profile import artifacts_dir, evidence_path

    artifacts_dir(tmp_path).mkdir(parents=True)
    evidence_path(tmp_path).write_text(
        json.dumps(
            {
                "schema": 1,
                "evidence": {
                    "hotspots": [["src/a.py", 9], ["src/", 20], ["docs", 5]],
                },
            }
        ),
        encoding="utf-8",
    )
    assert cli._hotspot_paths(tmp_path) == {"src/a.py"}
