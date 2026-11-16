"""Provider-aware LLM parameter resolution — one resolver, every call site.

Reviews, profile synthesis, theme clustering, and the setup ping all talk to
the same endpoint; whichever provider is configured (plain API key or GitHub
Copilot) must resolve identically everywhere, or a provider works in one path
and fails in another (the learn-under-Copilot blocker).
"""

from __future__ import annotations

from spc.config import LlmConfig, SpcConfig


def resolve_provider_params(
    config: SpcConfig | None,
) -> tuple[str, str | None, dict[str, str]]:
    """Resolve (base_url, api_key, extra_headers) for the merged config.

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
        return copilot.prepare_llm_call(llm_config, oauth_token)
    from spc.userconfig import resolve_api_key

    return llm_config.base_url, resolve_api_key(llm_config), {}
