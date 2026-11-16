"""Tests for spc/llm.py — error redaction (item 4) and single-attempt mode (item 7)."""

from __future__ import annotations

import httpx
import pytest

from spc import llm


class _PostRecorder:
    """Callable standing in for httpx.post, recording calls and returning canned responses."""

    def __init__(self, responses: list[httpx.Response]) -> None:
        self.responses = responses
        self.calls = 0

    def __call__(self, url, json=None, headers=None, timeout=None):  # noqa: ANN001
        self.calls += 1
        return self.responses.pop(0)


@pytest.fixture(name="no_backoff")
def fixture_no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(llm, "_sleep_backoff", lambda attempt: None)


def test_error_text_redacts_api_key(no_backoff: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """A hostile endpoint echoing the Authorization value must not leak it via LlmError."""
    secret = "sk-super-secret-value-987654"
    body = f'{{"error": "bad key {secret} denied"}}'
    recorder = _PostRecorder([httpx.Response(401, text=body)])
    monkeypatch.setattr(llm.httpx, "post", recorder)

    with pytest.raises(llm.LlmError) as exc_info:
        llm.complete(
            [{"role": "user", "content": "hi"}],
            model="m",
            base_url="https://llm.example/v1",
            api_key=secret,
            attempts=1,
        )
    assert secret not in str(exc_info.value)
    assert "***" in str(exc_info.value)


def test_retryable_error_redacts_key_too(
    no_backoff: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "sk-leaky-abcdef123456"
    recorder = _PostRecorder(
        [
            httpx.Response(500, text=f"oops {secret}"),
            httpx.Response(500, text=f"oops {secret}"),
            httpx.Response(500, text=f"oops {secret}"),
        ]
    )
    monkeypatch.setattr(llm.httpx, "post", recorder)

    with pytest.raises(llm.LlmError) as exc_info:
        llm.complete(
            [{"role": "user", "content": "hi"}],
            model="m",
            base_url="https://llm.example/v1",
            api_key=secret,
        )
    assert secret not in str(exc_info.value)


def test_attempts_one_makes_exactly_one_call(
    no_backoff: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """attempts=1 (ping path) must not retry — one hanging endpoint, one call."""
    recorder = _PostRecorder([httpx.Response(503, text="overloaded")])
    monkeypatch.setattr(llm.httpx, "post", recorder)

    with pytest.raises(llm.LlmError) as exc_info:
        llm.complete(
            [{"role": "user", "content": "hi"}],
            model="m",
            base_url="https://llm.example/v1",
            api_key=None,
            attempts=1,
        )
    assert recorder.calls == 1
    assert "after 1 attempts" in str(exc_info.value)


def test_default_retries_three_times(no_backoff: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """Default (review path) keeps the 3-attempt retry ladder."""
    recorder = _PostRecorder(
        [httpx.Response(500, text="x"), httpx.Response(500, text="x"), httpx.Response(500, text="x")]
    )
    monkeypatch.setattr(llm.httpx, "post", recorder)

    with pytest.raises(llm.LlmError):
        llm.complete(
            [{"role": "user", "content": "hi"}],
            model="m",
            base_url="https://llm.example/v1",
            api_key=None,
        )
    assert recorder.calls == 3


def test_redact_without_key_leaves_text_untouched() -> None:
    assert llm._redact("plain error", None) == "plain error"
    assert llm._redact("plain error", "") == "plain error"
