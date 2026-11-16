"""Provider-aware LLM param resolution (spc/provider.py).

The blocker this guards against: with provider="copilot", learn synthesis and
theme clustering used the plain-key path — no Authorization header, stored
copilot base URL → HTTP 400. Every LLM call site must resolve the same way.
"""

from __future__ import annotations

from typing import Any

import pytest

from spc.config import CopilotConfig, LlmConfig, SpcConfig
from spc.provider import resolve_provider_params


def _config(provider: str = "generic", **kwargs: Any) -> SpcConfig:
    return SpcConfig(
        llm=LlmConfig(provider=provider, **kwargs),
        copilot=CopilotConfig(github_oauth_token=""),
    )


class TestNonCopilotPassthrough:
    def test_generic_provider_uses_stored_base_url_and_resolved_key(self) -> None:
        base_url, api_key, extra_headers = resolve_provider_params(
            _config(base_url="https://llm.example/v1")
        )
        assert base_url == "https://llm.example/v1"
        assert api_key is None  # no key stored/env in this test run
        assert extra_headers == {}

    def test_non_copilot_never_touches_copilot_modules(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression guard: the plain-key path must not import copilot code."""

        def boom(*args: object, **kwargs: object) -> None:
            raise AssertionError("copilot machinery reached on a generic provider")

        monkeypatch.setattr("spc.copilot.prepare_llm_call", boom)
        monkeypatch.setattr("spc.userconfig.resolve_copilot_token", boom)
        base_url, api_key, extra_headers = resolve_provider_params(_config())
        assert base_url == LlmConfig().base_url
        assert extra_headers == {}


class TestCopilotResolution:
    def test_copilot_exchanges_and_returns_exchange_values(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "spc.userconfig.resolve_copilot_token", lambda _repo: "gho_OAUTH"
        )
        monkeypatch.setattr(
            "spc.copilot.prepare_llm_call",
            lambda llm, oauth: (
                "https://api.example-copilot/v1",
                "tid_COPILOT",
                {"Openai-Intent": "conversation-edits", "x-initiator": "user"},
            ),
        )
        base_url, api_key, extra_headers = resolve_provider_params(
            _config(provider="copilot")
        )
        assert base_url == "https://api.example-copilot/v1"
        assert api_key == "tid_COPILOT"
        assert extra_headers["Openai-Intent"] == "conversation-edits"
        assert extra_headers["x-initiator"] == "user"

    def test_copilot_error_propagates_unchanged(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fail-soft contract: callers catch CopilotError like LlmError."""
        from spc.copilot import CopilotError

        def boom(llm: LlmConfig, oauth: str | None) -> tuple[str, str, dict[str, str]]:
            raise CopilotError("no oauth token — run `spc setup`")

        monkeypatch.setattr(
            "spc.userconfig.resolve_copilot_token", lambda _repo: None
        )
        monkeypatch.setattr("spc.copilot.prepare_llm_call", boom)
        with pytest.raises(CopilotError):
            resolve_provider_params(_config(provider="copilot"))


class TestBuildProfileUsesSharedResolver:
    def test_profile_synthesis_under_copilot(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The learn path must go through the copilot exchange (was the blocker)."""
        from spc import llm
        from spc.githistory import GitEvidence
        from spc.profile import build_profile

        config = SpcConfig(
            llm=LlmConfig(provider="copilot"),
            copilot=CopilotConfig(github_oauth_token=""),
        )
        captured: dict[str, object] = {}

        def fake_complete(messages, **kwargs):  # noqa: ANN001, ANN003
            captured.update(kwargs)
            return "# profile"

        monkeypatch.setattr(llm, "complete", fake_complete)
        monkeypatch.setattr(
            "spc.userconfig.resolve_copilot_token", lambda _repo: "gho_OAUTH"
        )

        def fake_prepare(llm_config, oauth):
            assert oauth == "gho_OAUTH"
            return (
                "https://api.example-copilot/v1",
                "tid_COPILOT",
                {"Openai-Intent": "conversation-edits"},
            )

        monkeypatch.setattr("spc.copilot.prepare_llm_call", fake_prepare)
        out = build_profile(GitEvidence(), config)
        assert out == "# profile"
        assert captured["base_url"] == "https://api.example-copilot/v1"
        assert captured["api_key"] == "tid_COPILOT"
        headers = captured["extra_headers"]
        assert headers is not None and headers["Openai-Intent"] == "conversation-edits"


class TestClusterThemesUsesSharedResolver:
    def test_clustering_under_copilot(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Theme clustering must go through the copilot exchange too."""
        from spc import llm
        from spc.githubhistory import GithubEvidence, PrRecord, cluster_themes

        config = SpcConfig(
            llm=LlmConfig(provider="copilot"),
            copilot=CopilotConfig(github_oauth_token=""),
        )
        evidence = GithubEvidence(
            owner="o",
            repo="r",
            pulls=[PrRecord(number=1, title="t", merged_at="2026-01-01")],
            comments=[],
        )
        captured: dict[str, object] = {}

        def fake_complete(messages, **kwargs):  # noqa: ANN001, ANN003
            captured.update(kwargs)
            return "- theme"

        monkeypatch.setattr(llm, "complete", fake_complete)
        monkeypatch.setattr(
            "spc.userconfig.resolve_copilot_token", lambda _repo: "gho_OAUTH"
        )

        def fake_prepare(llm_config, oauth):
            assert oauth == "gho_OAUTH"
            return (
                "https://api.example-copilot/v1",
                "tid_COPILOT",
                {"x-initiator": "user"},
            )

        monkeypatch.setattr("spc.copilot.prepare_llm_call", fake_prepare)
        assert cluster_themes(evidence, config) == "- theme"
        assert captured["base_url"] == "https://api.example-copilot/v1"
        assert captured["api_key"] == "tid_COPILOT"
        headers = captured["extra_headers"]
        assert headers is not None and headers["x-initiator"] == "user"

    def test_clustering_generic_provider_unchanged(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression guard: non-copilot clustering keeps plain-key behavior."""
        from spc import llm
        from spc.githubhistory import GithubEvidence, PrRecord, cluster_themes

        config = _config(base_url="https://llm.example/v1")
        evidence = GithubEvidence(
            owner="o",
            repo="r",
            pulls=[PrRecord(number=1, title="t", merged_at="2026-01-01")],
            comments=[],
        )
        captured: dict[str, object] = {}

        def fake_complete(messages, **kwargs):  # noqa: ANN001, ANN003
            captured.update(kwargs)
            return "- theme"

        monkeypatch.setattr(llm, "complete", fake_complete)
        assert cluster_themes(evidence, config) == "- theme"
        assert captured["base_url"] == "https://llm.example/v1"
        assert captured["extra_headers"] is None  # no copilot headers injected
