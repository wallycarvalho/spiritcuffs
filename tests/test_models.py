"""Tests for spc/models.py — live model listing with chat-completions filtering.

The hard requirement under test: never offer a model the chat-completions
process will reject (embeddings dropped, responses-only Copilot models
dropped, transport failures raise ModelsError so the wizard can fail soft).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from spc import models
from spc.models import ModelsError, list_chat_models


class _GetRecorder:
    """Callable standing in for httpx.get, recording calls and returning canned responses."""

    def __init__(self, response: httpx.Response | Exception) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []

    def __call__(self, url, headers=None, timeout=None):  # noqa: ANN001
        self.calls.append({"url": url, "headers": headers, "timeout": timeout})
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def _json_response(payload: dict[str, Any], status_code: int = 200) -> httpx.Response:
    return httpx.Response(
        status_code, content=json.dumps(payload).encode(), request=httpx.Request("GET", "https://stub.local")
    )


@pytest.fixture(name="stub_get")
def fixture_stub_get(monkeypatch: pytest.MonkeyPatch) -> Callable[..., _GetRecorder]:
    """Replace spc.models' httpx.get with a recorder; returns the recorder factory."""
    active: dict[str, _GetRecorder | None] = {"rec": None}

    def install(response: httpx.Response | Exception) -> _GetRecorder:
        recorder = _GetRecorder(response)
        monkeypatch.setattr(models.httpx, "get", recorder)
        active["rec"] = recorder
        return recorder

    install.active = active  # type: ignore[attr-defined]
    return install


def _last_call(stub_get: Callable[..., _GetRecorder]) -> dict[str, Any]:
    """The most recent httpx.get call captured by the active recorder."""
    recorder = stub_get.active["rec"]  # type: ignore[attr-defined]
    assert recorder is not None and recorder.calls, "expected at least one httpx.get call"
    return recorder.calls[-1]


# ---------------------------------------------------------------------------
# openai


def test_openai_happy_path_filters_non_chat(stub_get) -> None:
    body = {
        "data": [
            {"id": "gpt-6-luna"},
            {"id": "text-embedding-3-small"},
            {"id": "whisper-1"},
            {"id": "tts-1"},
            {"id": "dall-e-3"},
            {"id": "gpt-4o-mini"},
        ]
    }
    stub_get(_json_response(body))
    result = list_chat_models("openai", "https://api.openai.com/v1", api_key="sk-test")
    assert result == ["gpt-6-luna", "gpt-4o-mini"]
    call = stub_get and _last_call(stub_get)
    assert call["url"] == "https://api.openai.com/v1/models"
    assert call["headers"]["Authorization"] == "Bearer sk-test"


def test_openai_keeps_unsure_names(stub_get) -> None:
    """Filtering is conservative: names that merely contain an innocuous substring stay."""
    stub_get(_json_response({"data": [{"id": "o4-mini"}, {"id": "audio-transcribe-x"}]}))
    result = list_chat_models("openai", "https://api.openai.com/v1")
    # "audio" pattern drops the transcribe model even though we're unsure
    assert result == ["o4-mini"]


def test_openai_no_key_omits_auth_header(stub_get) -> None:
    stub_get(_json_response({"data": [{"id": "gpt-6-luna"}]}))
    list_chat_models("openai", "https://api.openai.com/v1")
    assert "Authorization" not in _last_call(stub_get)["headers"]


# ---------------------------------------------------------------------------
# openrouter


def test_openrouter_keeps_text_modality_top_level(stub_get) -> None:
    body = {
        "data": [
            {"id": "anthropic/claude-3.5-haiku", "output_modalities": ["text"]},
            {"id": "openai/gpt-4o-mini", "architecture": {"output_modalities": ["text", "image"]}},
            {"id": "vendor/image-only", "output_modalities": ["image"]},
        ]
    }
    stub_get(_json_response(body))
    result = list_chat_models("openrouter", "https://openrouter.ai/api/v1")
    assert result == ["anthropic/claude-3.5-haiku", "openai/gpt-4o-mini"]


def test_openrouter_missing_modality_field_keeps_model(stub_get) -> None:
    stub_get(_json_response({"data": [{"id": "legacy/model"}, {"id": "ok/model", "output_modalities": ["text"]}]}))
    assert list_chat_models("openrouter", "https://openrouter.ai/api/v1") == [
        "legacy/model",
        "ok/model",
    ]


def test_openrouter_sends_bearer_when_key_present(stub_get) -> None:
    stub_get(_json_response({"data": [{"id": "m/1"}]}))
    list_chat_models("openrouter", "https://openrouter.ai/api/v1", api_key="sk-or-123")
    assert _last_call(stub_get)["headers"]["Authorization"] == "Bearer sk-or-123"


# ---------------------------------------------------------------------------
# anthropic


def test_anthropic_happy_path_no_filter(stub_get) -> None:
    stub_get(_json_response({"data": [{"id": "claude-3-5-haiku-latest"}, {"id": "claude-sonnet-4-5"}]}))
    result = list_chat_models("anthropic", "https://api.anthropic.com/v1", api_key="sk-ant")
    assert result == ["claude-3-5-haiku-latest", "claude-sonnet-4-5"]
    call = _last_call(stub_get)
    assert call["url"] == "https://api.anthropic.com/v1/v1/models"
    assert call["headers"]["x-api-key"] == "sk-ant"
    assert call["headers"]["anthropic-version"] == "2023-06-01"


# ---------------------------------------------------------------------------
# copilot


def _copilot_entry(model_id: str, endpoints: dict[str, Any] | None) -> dict[str, Any]:
    entry: dict[str, Any] = {"id": model_id}
    if endpoints is not None:
        entry.update(endpoints)
    return entry


@pytest.mark.parametrize("field", ["endpoints", "supported_endpoints"])
def test_copilot_endpoints_field_variants(stub_get, field: str) -> None:
    """Both observed field shapes work; only /chat_completions entries survive."""
    body = {
        "data": [
            _copilot_entry("gpt-4.1", {field: ["/chat_completions"]}),
            _copilot_entry("gpt-5.6-luna", {field: ["/responses"]}),
        ]
    }
    stub_get(_json_response(body))
    result = list_chat_models("copilot", "https://api.githubcopilot.com", api_key="tok")
    assert result == ["gpt-4.1"]


def test_copilot_uses_chat_headers_and_token_api_base(stub_get) -> None:
    from spc.copilot import COPILOT_CHAT_HEADERS, CopilotToken

    stub_get(_json_response({"data": [{"id": "gpt-4.1", "endpoints": ["/chat_completions"]}]}))
    token = CopilotToken(token="shortlived", api_base="https://api.individual.githubcopilot.com")
    result = list_chat_models("copilot", "https://api.githubcopilot.com", copilot_token=token)
    assert result == ["gpt-4.1"]
    call = _last_call(stub_get)
    assert call["url"] == "https://api.individual.githubcopilot.com/models"
    assert call["headers"]["Authorization"] == "Bearer shortlived"
    for header, value in COPILOT_CHAT_HEADERS.items():
        assert call["headers"][header] == value


def test_copilot_entry_without_endpoints_field_is_kept(stub_get) -> None:
    """Field absent entirely → cannot prove it chat-incapable → keep (ping gates)."""
    stub_get(_json_response({"data": [{"id": "mystery-model"}]}))
    assert list_chat_models("copilot", "https://api.githubcopilot.com", api_key="t") == [
        "mystery-model"
    ]


# ---------------------------------------------------------------------------
# ollama


def test_ollama_filters_embed_families_keeps_rest_verbatim(stub_get) -> None:
    body = {
        "models": [
            {"name": "llama3.1:latest", "digest": "abc"},
            {"name": "nomic-embed-text", "details": {"family": "embed"}},
            {"name": "bge-m3"},
            {"name": "qwen2.5-coder:7b"},
        ]
    }
    stub_get(_json_response(body))
    result = list_chat_models("ollama", "http://localhost:11434/v1")
    assert result == ["llama3.1:latest", "qwen2.5-coder:7b"]


def test_ollama_hits_tags_endpoint(stub_get) -> None:
    stub_get(_json_response({"models": [{"name": "llama3.1"}]}))
    list_chat_models("ollama", "http://localhost:11434/v1")
    assert _last_call(stub_get)["url"] == "http://localhost:11434/v1/api/tags"


# ---------------------------------------------------------------------------
# fail-soft transport / parsing


def test_timeout_raises_models_error(stub_get) -> None:
    stub_get(httpx.ConnectTimeout("timed out"))
    with pytest.raises(ModelsError, match="request failed"):
        list_chat_models("openai", "https://api.openai.com/v1")


def test_http_500_raises_models_error(stub_get) -> None:
    stub_get(
        httpx.Response(500, request=httpx.Request("GET", "https://stub.local"), text="boom")
    )
    with pytest.raises(ModelsError):
        list_chat_models("openai", "https://api.openai.com/v1")


def test_malformed_json_raises_models_error(stub_get) -> None:
    stub_get(
        httpx.Response(
            200, content=b"<html>not json</html>", request=httpx.Request("GET", "https://stub.local")
        )
    )
    with pytest.raises(ModelsError, match="not valid JSON"):
        list_chat_models("openai", "https://api.openai.com/v1")


def test_non_object_json_raises_models_error(stub_get) -> None:
    stub_get(
        httpx.Response(200, content=b"[1, 2, 3]", request=httpx.Request("GET", "https://stub.local"))
    )
    with pytest.raises(ModelsError, match="not a JSON object"):
        list_chat_models("openai", "https://api.openai.com/v1")


def test_missing_data_array_raises_models_error(stub_get) -> None:
    stub_get(_json_response({"unexpected": True}))
    with pytest.raises(ModelsError, match="missing data"):
        list_chat_models("openai", "https://api.openai.com/v1")


def test_unknown_provider_kind_raises(stub_get) -> None:
    with pytest.raises(ModelsError, match="no model listing"):
        list_chat_models("widgetcorp", "https://x.example")


def test_timeout_is_15_seconds(stub_get) -> None:
    stub_get(_json_response({"data": []}))
    list_chat_models("openai", "https://api.openai.com/v1")
    assert _last_call(stub_get)["timeout"] == 15.0
