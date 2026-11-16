"""Provider-aware LLM param resolution (spc/provider.py).

The blocker this guards against: with provider="copilot", learn synthesis and
theme clustering used the plain-key path — no Authorization header, stored
copilot base URL → HTTP 400. Every LLM call site must resolve the same way.
"""

from __future__ import annotations

from typing import Any

import pytest

from spc.config import CopilotConfig, LlmConfig, SpcConfig
from spc.provider import ProviderParams, resolve_provider_params


def _config(provider: str = "generic", **kwargs: Any) -> SpcConfig:
    return SpcConfig(
        llm=LlmConfig(provider=provider, **kwargs),
        copilot=CopilotConfig(github_oauth_token=""),
    )


class TestNonCopilotPassthrough:
    def test_generic_provider_uses_stored_base_url_and_resolved_key(self) -> None:
        params = resolve_provider_params(_config(base_url="https://llm.example/v1"))
        assert params.base_url == "https://llm.example/v1"
        assert params.api_key is None  # no key stored/env in this test run
        assert params.extra_headers == {}
        assert params.copilot_fallback is False

    def test_non_copilot_never_touches_copilot_modules(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression guard: the plain-key path must not import copilot code."""

        def boom(*args: object, **kwargs: object) -> None:
            raise AssertionError("copilot machinery reached on a generic provider")

        monkeypatch.setattr("spc.copilot.prepare_call_parts", boom)
        monkeypatch.setattr("spc.copilot.prepare_llm_call", boom)
        monkeypatch.setattr("spc.userconfig.resolve_copilot_token", boom)
        params = resolve_provider_params(_config())
        assert params.base_url == LlmConfig().base_url
        assert params.extra_headers == {}


class TestCopilotResolution:
    def test_copilot_exchanges_and_returns_exchange_values(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from spc.copilot import CopilotToken

        monkeypatch.setattr(
            "spc.userconfig.resolve_copilot_token", lambda _repo: "gho_OAUTH"
        )
        monkeypatch.setattr(
            "spc.copilot.prepare_call_parts",
            lambda llm, oauth: (
                CopilotToken(
                    token="tid_COPILOT", api_base="https://api.example-copilot/v1"
                ),
                {"Openai-Intent": "conversation-panel", "x-initiator": "user"},
            ),
        )
        params = resolve_provider_params(_config(provider="copilot"))
        assert params.base_url == "https://api.example-copilot/v1"
        assert params.api_key == "tid_COPILOT"
        assert params.extra_headers["Openai-Intent"] == "conversation-panel"
        assert params.extra_headers["x-initiator"] == "user"
        assert params.copilot_fallback is False

    def test_copilot_fallback_flags_degraded_mode(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Raw-OAuth fallback (exchange 403/404) must be observable, not silent."""
        from spc.copilot import CopilotToken

        monkeypatch.setattr(
            "spc.userconfig.resolve_copilot_token", lambda _repo: "ghu_OAUTH"
        )
        monkeypatch.setattr(
            "spc.copilot.prepare_call_parts",
            lambda llm, oauth: (
                CopilotToken(
                    token="ghu_OAUTH",
                    api_base="https://api.githubcopilot.com",
                    exchanged=False,
                ),
                {"x-initiator": "user"},
            ),
        )
        params = resolve_provider_params(_config(provider="copilot"))
        assert params.copilot_fallback is True
        assert params.api_key == "ghu_OAUTH"

    def test_copilot_error_propagates_unchanged(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fail-soft contract: callers catch CopilotError like LlmError."""
        from spc.copilot import CopilotError

        def boom(llm: LlmConfig, oauth: str | None) -> tuple[object, dict[str, str]]:
            raise CopilotError("no oauth token — run `spc setup`")

        monkeypatch.setattr(
            "spc.userconfig.resolve_copilot_token", lambda _repo: None
        )
        monkeypatch.setattr("spc.copilot.prepare_call_parts", boom)
        with pytest.raises(CopilotError):
            resolve_provider_params(_config(provider="copilot"))


class TestBuildProfileUsesSharedResolver:
    def test_profile_synthesis_under_copilot(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The learn path must go through the copilot exchange (was the blocker)."""
        from spc import llm
        from spc.copilot import CopilotToken
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
            return CopilotToken(
                token="tid_COPILOT", api_base="https://api.example-copilot/v1"
            ), {"Openai-Intent": "conversation-panel"}

        monkeypatch.setattr("spc.copilot.prepare_call_parts", fake_prepare)
        out = build_profile(GitEvidence(), config)
        assert out == "# profile"
        assert captured["base_url"] == "https://api.example-copilot/v1"
        assert captured["api_key"] == "tid_COPILOT"
        headers = captured["extra_headers"]
        assert headers is not None and headers["Openai-Intent"] == "conversation-panel"


class TestClusterThemesUsesSharedResolver:
    def test_clustering_under_copilot(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Theme clustering must go through the copilot exchange too."""
        from spc import llm
        from spc.copilot import CopilotToken
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
            return CopilotToken(
                token="tid_COPILOT", api_base="https://api.example-copilot/v1"
            ), {"x-initiator": "user"}

        monkeypatch.setattr("spc.copilot.prepare_call_parts", fake_prepare)
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


def test_provider_params_shape_is_stable() -> None:
    """The resolver's return type: attributes, defaults, fail-soft contract."""
    params = ProviderParams(base_url="https://x", api_key="k", extra_headers={})
    assert params.copilot_fallback is False  # default: healthy mode
