"""spc learn end-to-end (git mining + mocked LLM synthesis) and fail-soft paths."""

from __future__ import annotations

from pathlib import Path

import pytest
from rich.console import Console

from spc.config import LearnConfig, SpcConfig
from spc.learn import run_learn
from spc.profile import load_evidence, profile_path


@pytest.fixture()
def quiet_console() -> Console:
    return Console(file=open("/dev/null", "w"))  # noqa: SIM115 — tests only


def test_learn_writes_evidence_and_profile(git_repo, monkeypatch, quiet_console) -> None:
    from spc import llm as llm_mod

    repo = git_repo(
        {"src/a.py": "a=1\n"},
        later_commits=[("fix: crash", {"src/a.py": "a=2\n"})],
    )
    monkeypatch.delenv("SPC_API_KEY", raising=False)

    captured = {}

    def fake_complete(messages, *, model, base_url, api_key, **kw):  # noqa: ANN001, ANN003
        captured["prompt"] = messages[-1]["content"]
        captured["model"] = model
        return "# Profile\nThis repo's hotspot is `src/a.py`."

    monkeypatch.setattr(llm_mod.httpx, "post", _raise_no_network)
    # Patch at the profile layer boundary: build_profile calls llm.complete.
    import spc.profile as profile_mod

    monkeypatch.setattr(profile_mod.llm, "complete", fake_complete)

    config = SpcConfig()
    assert run_learn(repo, config, quiet_console) is True

    evidence = load_evidence(repo)
    assert evidence is not None
    assert evidence["commits_scanned"] == 2
    assert profile_path(repo).read_text(encoding="utf-8").startswith("# Profile")
    assert "hotspots" in captured["prompt"]
    assert captured["model"] == config.llm.model


def _raise_no_network(*args, **kwargs):  # noqa: ANN002, ANN003
    raise AssertionError("network httpx.post must not be called in tests")


def test_learn_llm_failure_is_fail_soft_no_profile(git_repo, monkeypatch, quiet_console) -> None:
    import spc.profile as profile_mod

    repo = git_repo({"src/a.py": "a=1\n"})
    monkeypatch.setattr(
        profile_mod.llm, "complete", _always_raise
    )
    config = SpcConfig()
    assert run_learn(repo, config, quiet_console) is False
    assert not profile_path(repo).exists()  # NO fallback profile


def _always_raise(*args, **kwargs):  # noqa: ANN002, ANN003
    from spc.llm import LlmError

    raise LlmError("boom")


def test_learn_no_git_repo_fails_soft(tmp_path: Path, quiet_console) -> None:
    config = SpcConfig()
    assert run_learn(tmp_path, config, quiet_console) is False


def test_learn_prompt_never_contains_secrets(git_repo, monkeypatch, quiet_console) -> None:
    """Historical excerpts may carry code, but never an unredacted secret."""
    import spc.profile as profile_mod

    secret_value = "AKIA0123456789ABCDEF"
    repo = git_repo(
        {"src/a.py": f'key = "{secret_value}"\n'},
        later_commits=[("fix: thing", {"src/a.py": "key = None\n"})],
    )
    prompts = []

    def fake_complete(messages, *, model, base_url, api_key, **kw):  # noqa: ANN001, ANN003
        prompts.append(messages[-1]["content"])
        return "# p"

    monkeypatch.setattr(profile_mod.llm, "complete", fake_complete)
    run_learn(repo, SpcConfig(), quiet_console)
    assert prompts, "synthesis must be called"
    assert secret_value not in prompts[0]
    assert "[REDACTED" in prompts[0]


def test_learn_prompt_omits_code_when_excerpts_disabled(
    git_repo, monkeypatch, quiet_console
) -> None:
    """``learn.send_code_excerpts = false`` keeps every line of source out."""
    import spc.profile as profile_mod

    repo = git_repo(
        {"src/a.py": "MARKER_LINE_ONE = 1\n"},
        later_commits=[("fix: thing", {"src/a.py": "MARKER_LINE_TWO = 2\n"})],
    )
    prompts = []

    def fake_complete(messages, *, model, base_url, api_key, **kw):  # noqa: ANN001, ANN003
        prompts.append(messages[-1]["content"])
        return "# p"

    monkeypatch.setattr(profile_mod.llm, "complete", fake_complete)
    config = SpcConfig(learn=LearnConfig(send_code_excerpts=False))
    run_learn(repo, config, quiet_console)
    assert prompts, "synthesis must be called"
    assert "MARKER_LINE_ONE" not in prompts[0]
    assert "MARKER_LINE_TWO" not in prompts[0]


def test_profile_drops_lines_that_quote_excerpt_code(
    git_repo, monkeypatch, quiet_console
) -> None:
    """The documented leak filter must actually run on the synthesized profile.

    A quoted excerpt would otherwise be written into ``.spc/profile.md`` and
    injected into every later review prompt — a wider leak than the one-off
    learn call the code was read for.
    """
    import spc.profile as profile_mod

    repo = git_repo(
        {"src/a.py": "QUOTABLE_CODE_LINE_ONE = 1\n"},
        later_commits=[("fix: thing", {"src/a.py": "QUOTABLE_CODE_LINE_ONE = 2\n"})],
    )

    def fake_complete(messages, *, model, base_url, api_key, **kw):  # noqa: ANN001, ANN003
        return (
            "# Repo profile\n\n"
            "## Recurring bug patterns\n"
            "- Off-by-one in totals (`QUOTABLE_CODE_LINE_ONE = 2`) keeps reappearing.\n"
            "- Keep an eye on the invoice rounding path.\n"
        )

    monkeypatch.setattr(profile_mod.llm, "complete", fake_complete)
    assert run_learn(repo, SpcConfig(), quiet_console) is True
    profile_md = profile_path(repo).read_text(encoding="utf-8")
    assert "QUOTABLE_CODE_LINE_ONE" not in profile_md
    assert "Keep an eye on the invoice rounding path." in profile_md


def test_synthesis_prompt_omits_stats_only_fields(git_repo, monkeypatch, quiet_console) -> None:
    """Language proportions / test co-change live in evidence.json, not the prompt.

    Restating them is what made profiles generic — the LLM parroted the stats
    back ("Python 75.6%") instead of surfacing learned review knowledge.
    """
    import spc.profile as profile_mod

    repo = git_repo({"src/a.py": "a=1\n"})
    prompts = []

    def fake_complete(messages, *, model, base_url, api_key, **kw):  # noqa: ANN001, ANN003
        prompts.append(messages[-1]["content"])
        return "# p"

    monkeypatch.setattr(profile_mod.llm, "complete", fake_complete)
    run_learn(repo, SpcConfig(), quiet_console)
    assert prompts, "synthesis must be called"
    assert "language_proportions" not in prompts[0]
    assert "test_co_change_ratio" not in prompts[0]


def test_synthesis_system_bans_generic_advice() -> None:
    """The ban list must survive future edits to the system prompt.

    Generic, true-of-every-repo advice ("ensure tests accompany changes") was
    the other half of the useless-profile problem; these assertions pin the
    ban so a refactor can't silently drop it.
    """
    from spc.profile import _SYNTHESIS_SYSTEM

    assert "any repository" in _SYNTHESIS_SYSTEM
    assert "ensure tests" in _SYNTHESIS_SYSTEM
    assert "keep documentation up to date" in _SYNTHESIS_SYSTEM
    assert "regressions in complex code" in _SYNTHESIS_SYSTEM
    assert "Do NOT include statistics" in _SYNTHESIS_SYSTEM
    assert "Do NOT state language composition" in _SYNTHESIS_SYSTEM
    # The evidence-backed-over-padded contract, and the required sections.
    assert "beats a padded one" in _SYNTHESIS_SYSTEM
    for section in ("Peer review themes", "Recurring bug patterns", "Non-obvious conventions", "What to double-check"):
        assert section in _SYNTHESIS_SYSTEM


def test_cluster_system_asks_for_checkable_rules() -> None:
    """Clustering must surface repo-specific rules, not just topic counts."""
    from spc.githubhistory import _CLUSTER_SYSTEM

    assert "rules" in _CLUSTER_SYSTEM
    assert "check a diff against" in _CLUSTER_SYSTEM
    # Safety rules survive the extension.
    assert "never quote" in _CLUSTER_SYSTEM
    assert "never mention authors" in _CLUSTER_SYSTEM
    assert "under 200 words" in _CLUSTER_SYSTEM
