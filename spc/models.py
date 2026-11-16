"""Live chat-model listing for the setup wizard — one function, five providers.

Queries each provider's model-list endpoint and returns ONLY models that can
serve a chat-completions request. The hard requirement: never offer a model
the chat-completions process will reject (the gpt-5.6-luna lesson — it is
responses-only on Copilot and 400s on chat). Filtering happens up front here;
the wizard's ping stays the final gate.

Every failure raises :class:`ModelsError` — the wizard catches it, prints one
line, and falls back to the curated shortlist. Fail-soft by construction.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from spc.copilot import CopilotToken

LIST_TIMEOUT_SECONDS = 15.0

ANTHROPIC_VERSION = "2023-06-01"

# OpenAI's /models list does not advertise endpoint support, so name-shaped
# families are dropped. Conservative by design: when unsure, INCLUDE — the
# ping is the final gate and a false exclusion hides a working chat model,
# while a false inclusion just fails visibly at the ping.
_NON_CHAT_PATTERNS: tuple[str, ...] = (
    "embedding", "embed", "whisper", "tts", "audio", "moderation",
    "davinci", "babbage", "image", "dall-e",
)


class ModelsError(RuntimeError):
    """Raised when the model list cannot be fetched or parsed."""


def list_chat_models(
    provider_kind: str,
    base_url: str,
    api_key: str | None = None,
    copilot_token: CopilotToken | None = None,
) -> list[str]:
    """Fetch the provider's model list, filtered to chat-completions-capable ids.

    *provider_kind* is one of "openai" | "openrouter" | "anthropic" |
    "copilot" | "ollama". *copilot_token* is a ``spc.copilot.CopilotToken``
    (its ``token`` and ``api_base`` override *api_key*/*base_url*). Raises
    :class:`ModelsError` on timeout, HTTP error, or malformed body — never
    returns a half-parsed list.
    """
    fetchers = {
        "openai": _fetch_openai,
        "openrouter": _fetch_openrouter,
        "anthropic": _fetch_anthropic,
        "copilot": _fetch_copilot,
        "ollama": _fetch_ollama,
    }
    fetcher = fetchers.get(provider_kind)
    if fetcher is None:
        raise ModelsError(f"no model listing for provider {provider_kind!r}")
    return fetcher(
        base_url=base_url,
        api_key=api_key,
        copilot_token=copilot_token,
    )


def _get_json(url: str, headers: dict[str, str]) -> dict[str, Any]:
    """GET *url* and return the parsed JSON object; anything else is ModelsError."""
    try:
        response = httpx.get(url, headers=headers, timeout=LIST_TIMEOUT_SECONDS)
        response.raise_for_status()
        body = response.json()
    except httpx.HTTPError as exc:
        raise ModelsError(f"model list request failed ({type(exc).__name__})") from exc
    except ValueError as exc:
        raise ModelsError("model list response was not valid JSON") from exc
    if not isinstance(body, dict):
        raise ModelsError("model list response was not a JSON object")
    return body


def _extract_ids(body: dict[str, Any]) -> list[str]:
    """Pull string model ids out of a ``data[]`` payload (OpenAI-family shape)."""
    entries = body.get("data")
    if not isinstance(entries, list):
        raise ModelsError("model list response missing data[]")
    ids: list[str] = []
    for entry in entries:
        if isinstance(entry, dict) and isinstance(entry.get("id"), str) and entry["id"]:
            ids.append(entry["id"])
    return ids


def _fetch_openai(
    *, base_url: str, api_key: str | None, copilot_token: CopilotToken | None,
) -> list[str]:
    """OpenAI: GET {base}/models with Bearer auth; filter non-chat name families."""
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    ids = _extract_ids(_get_json(f"{base_url.rstrip('/')}/models", headers))
    return [model_id for model_id in ids if not _is_non_chat_name(model_id)]


def _is_non_chat_name(model_id: str) -> bool:
    """True when the model id names a non-chat family (embeddings, audio, …)."""
    lowered = model_id.lower()
    return any(pattern in lowered for pattern in _NON_CHAT_PATTERNS)


def _fetch_openrouter(
    *, base_url: str, api_key: str | None, copilot_token: CopilotToken | None,
) -> list[str]:
    """OpenRouter: GET /models; keep ids whose output modalities include text."""
    headers: dict[str, str] = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    body = _get_json(f"{base_url.rstrip('/')}/models", headers)
    entries = body.get("data")
    if not isinstance(entries, list):
        raise ModelsError("model list response missing data[]")
    kept: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str) or not entry["id"]:
            continue
        modalities = _output_modalities(entry)
        # Field entirely missing → the payload predates modality metadata;
        # keep everything rather than hide working models (ping still gates).
        if modalities is None or "text" in modalities:
            kept.append(entry["id"])
    return kept


def _output_modalities(entry: dict[str, Any]) -> list[str] | None:
    """Read an OpenRouter entry's output modalities; None when the field is absent.

    Accepts both ``output_modalities`` (top level) and
    ``architecture.output_modalities``; malformed shapes count as absent so
    one odd entry never sinks the whole list.
    """
    architecture = entry.get("architecture")
    candidates = (
        entry.get("output_modalities"),
        architecture.get("output_modalities") if isinstance(architecture, dict) else None,
    )
    for candidate in candidates:
        if isinstance(candidate, list) and all(isinstance(item, str) for item in candidate):
            return candidate
    return None


def _fetch_anthropic(
    *, base_url: str, api_key: str | None, copilot_token: CopilotToken | None,
) -> list[str]:
    """Anthropic: GET {base}/models with x-api-key; every listed model chats.

    The provider base_url already ends in ``/v1`` (setup.py) — appending
    another ``/v1`` produced ``/v1/v1/models`` and a guaranteed 404.
    """
    headers = {
        "x-api-key": api_key or "",
        "anthropic-version": ANTHROPIC_VERSION,
    }
    ids = _extract_ids(_get_json(f"{base_url.rstrip('/')}/models", headers))
    return ids


def _fetch_copilot(
    *, base_url: str, api_key: str | None, copilot_token: CopilotToken | None,
) -> list[str]:
    """Copilot: GET {api_base}/models with the Copilot chat headers + Bearer token.

    Entries carry their supported endpoints; only models advertising chat
    completions are kept (this is what filters responses-only models such as
    gpt-5.6-luna, which 400 on chat completions). The endpoints field name
    has been observed as both ``endpoints`` and ``supported_endpoints`` —
    both list-of-strings shapes are accepted defensively.
    """
    from spc import copilot as copilot_module

    token = copilot_token.token if copilot_token is not None else api_key
    api_base = (
        copilot_token.api_base
        if copilot_token is not None and copilot_token.api_base
        else base_url
    )
    headers = {
        **copilot_module.COPILOT_CHAT_HEADERS,
        "User-Agent": copilot_module.USER_AGENT,
        "Authorization": f"Bearer {token}",
    }
    body = _get_json(f"{api_base.rstrip('/')}/models", headers)
    entries = body.get("data")
    if not isinstance(entries, list):
        raise ModelsError("model list response missing data[]")
    kept: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str) or not entry["id"]:
            continue
        supported = _supported_endpoints(entry)
        if supported is None or any("chat" in endpoint for endpoint in supported):
            kept.append(entry["id"])
    return kept


def _supported_endpoints(entry: dict[str, Any]) -> list[str] | None:
    """Read a Copilot entry's supported endpoints; None when no field is present.

    Handles both observed shapes, ``endpoints`` and ``supported_endpoints``,
    as a list of strings. A non-list or non-string list counts as absent —
    an unreadable entry must not silently drop a model the user may want.
    """
    for key in ("endpoints", "supported_endpoints"):
        value = entry.get(key)
        if isinstance(value, list) and all(isinstance(item, str) for item in value):
            return value
    return None


def _fetch_ollama(
    *, base_url: str, api_key: str | None, copilot_token: CopilotToken | None,
) -> list[str]:
    """Ollama: GET {base}/models (OpenAI-compat); drop embed families from the ids.

    The OpenAI-compatible ``{base}/models`` endpoint is used — not Ollama's
    native ``/api/tags``, which has no ``/v1`` prefix (the provider base_url
    ends in ``/v1``) and which LM Studio / vLLM do not serve at all. LM
    Studio and vLLM serve the same OpenAI-compat shape, so one endpoint
    covers all three local runtimes.
    """
    ids = _extract_ids(_get_json(f"{base_url.rstrip('/')}/models", {}))
    return [model_id for model_id in ids if not _is_embed_family(model_id)]


def _is_embed_family(name: str) -> bool:
    """True when an Ollama entry names an embedding model (embed / bge family)."""
    lowered = name.lower()
    return "embed" in lowered or "bge" in lowered
