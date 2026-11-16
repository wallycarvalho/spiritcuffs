"""Single-shot OpenAI-compatible HTTP client (httpx), with retry and timeout."""

from __future__ import annotations

import os
import time
from typing import Any

import httpx

DEFAULT_TIMEOUT_SECONDS = 30.0
MAX_ATTEMPTS = 3
RETRY_BASE_DELAY_SECONDS = 1.0
# Retriable statuses: rate limiting + server-side + request timeout.
# Other 4xx (401 auth, 400 bad request, …) are deterministic — retrying them
# just delays the same failure, so they fail fast.
RETRYABLE_STATUSES = frozenset({408, 429, 500, 502, 503, 504})


class LlmError(RuntimeError):
    """Raised when the LLM call fails after retries."""


def resolve_api_key(api_key_env: str) -> str | None:
    """Read the API key from the configured env var; None when unset/empty."""
    return os.environ.get(api_key_env) or None


def _redact(text: str, api_key: str | None) -> str:
    """Drop the API key from server-returned text — never echo credentials."""
    if not api_key:
        return text
    return text.replace(api_key, "***")


def complete(
    messages: list[dict[str, str]],
    *,
    model: str,
    base_url: str,
    api_key: str | None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    attempts: int = MAX_ATTEMPTS,
    extra_headers: dict[str, str] | None = None,
) -> str:
    """Send one chat-completion request and return the assistant text.

    Retries transient failures (429/5xx/timeouts) up to *attempts* times with
    linear backoff. Raises :class:`LlmError` when all attempts fail — callers
    are expected to fail soft. Pass ``attempts=1`` for single-shot probes
    (connection tests) that must not linger. *extra_headers* are merged last
    (provider-specific requirements, e.g. Copilot's Openai-Intent header).
    """
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    if extra_headers:
        headers.update(extra_headers)

    payload: dict[str, Any] = {"model": model, "messages": messages}
    url = f"{base_url.rstrip('/')}/chat/completions"

    last_error: str | None = None
    for attempt in range(1, attempts + 1):
        try:
            response = httpx.post(url, json=payload, headers=headers, timeout=timeout)
            if response.status_code in RETRYABLE_STATUSES:
                last_error = f"HTTP {response.status_code}: {_redact(response.text[:200], api_key)}"
                _sleep_backoff(attempt)
                continue
            if 400 <= response.status_code < 500:
                raise LlmError(
                    f"LLM request rejected (HTTP {response.status_code}): "
                    f"{_redact(response.text[:200], api_key)}"
                )
            response.raise_for_status()
            body = response.json()
            return _extract_text(body)
        except httpx.HTTPError as exc:
            last_error = f"{type(exc).__name__}: {_redact(str(exc), api_key)}"
            _sleep_backoff(attempt)
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise LlmError(f"unexpected LLM response shape: {exc}") from exc

    raise LlmError(f"LLM call failed after {attempts} attempts: {last_error}")


def _sleep_backoff(attempt: int) -> None:
    """Sleep before retrying *attempt* (1-based); caps out quickly to stay under 15s."""
    time.sleep(RETRY_BASE_DELAY_SECONDS * attempt)


def _extract_text(body: dict[str, Any]) -> str:
    """Pull the first choice's message text out of an OpenAI-style response."""
    choices = body["choices"]
    content = choices[0]["message"]["content"]
    if not isinstance(content, str):
        raise TypeError("assistant content is not a string")
    return content
