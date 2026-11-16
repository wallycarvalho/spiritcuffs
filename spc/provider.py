"""Provider-aware LLM parameter resolution — one resolver, every call site.

Reviews, profile synthesis, theme clustering, and the setup ping all talk to
the same endpoint; whichever provider is configured (plain API key or GitHub
Copilot) must resolve identically everywhere, or a provider works in one path
and fails in another (the learn-under-Copilot blocker).
"""

from __future__ import annotations

from dataclasses import dataclass

from spc.config import LlmConfig, SpcConfig


@dataclass(frozen=True)
class ProviderParams:
    """Resolved LLM call parameters, provider-independent.

    ``copilot_fallback`` is True when Copilot is running on the raw OAuth
    token (exchange rejected 403/404) — a degraded mode the wizard surfaces
    so the user can re-auth; reviews and learn stay silent (fail-soft).
    """

    base_url: str
    api_key: str | None
    extra_headers: dict[str, str]
    copilot_fallback: bool = False


def resolve_provider_params(config: SpcConfig | None) -> ProviderParams:
    """Resolve call parameters for the merged config.

    Copilot: exchanges the stored OAuth token for a short-lived Copilot token
    (raises ``copilot.CopilotError`` on failure — callers handle it exactly
    like ``llm.LlmError``). Non-copilot: plain key resolution
    (repo → user config → env). ``config=None`` behaves like a fresh default
    config (generic provider, no key).
    """
    llm_config = config.llm if config is not None else LlmConfig()
    if llm_config.provider == "copilot":
        from spc import copilot
        from spc.userconfig import resolve_copilot_token

        oauth_token = resolve_copilot_token(
            config.copilot.github_oauth_token if config is not None else None
        )
        copilot_token, extra_headers = copilot.prepare_call_parts(llm_config, oauth_token)
        return ProviderParams(
            base_url=copilot_token.api_base,
            api_key=copilot_token.token,
            extra_headers=extra_headers,
            copilot_fallback=not copilot_token.exchanged,
        )
    from spc.userconfig import resolve_api_key

    return ProviderParams(
        base_url=llm_config.base_url,
        api_key=resolve_api_key(llm_config),
        extra_headers={},
    )
