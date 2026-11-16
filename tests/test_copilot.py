"""Tests for spc/copilot.py and the Copilot provider wiring (all httpx mocked)."""

from __future__ import annotations

import io
import stat
from pathlib import Path

import httpx
import pytest

from spc import copilot, llm
from spc.config import CopilotConfig, LlmConfig, load_config
from spc.setup import PROVIDERS, run_setup
from spc.userconfig import (
    load_user_config,
    resolve_copilot_token,
    save_user_config,
    user_config_path,
)


def _console():
    """Off-screen rich console for output assertions."""
    from rich.console import Console

    return Console(file=io.StringIO(), width=200)


@pytest.fixture()
def user_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """User config path (already redirected by conftest) with token env cleared."""
    target = user_config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    for var in (
        "SPC_API_KEY",
        "COPILOT_GITHUB_TOKEN",
        "OPENROUTER_API_KEY",
        "OPENAI_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)
    return target


def _client(handler) -> httpx.Client:
    """httpx client backed by a MockTransport (never touches the network)."""
    return httpx.Client(transport=httpx.MockTransport(handler))


def _json_response(data: dict) -> httpx.Response:
    return httpx.Response(200, json=data)


class TestDeviceFlow:
    def test_request_device_code_parses_fields(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url == httpx.URL(copilot.DEVICE_CODE_URI)
            body = request.read().decode()
            assert "Ov23li5Rr1rgjMTpz5am" in body
            assert "read:user" in body
            return _json_response(
                {
                    "device_code": "DC123",
                    "user_code": "ABCD-1234",
                    "verification_uri": "https://github.com/login/device",
                    "interval": 5,
                    "expires_in": 900,
                }
            )

        device = copilot.request_device_code(_client(handler))
        assert device.device_code == "DC123"
        assert device.user_code == "ABCD-1234"
        assert device.verification_uri == "https://github.com/login/device"
        assert device.interval == 5

    def test_request_device_code_defaults_on_malformed(self) -> None:
        """Missing numeric fields fall back to sane defaults (never 0 → busy loop)."""
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
            return _json_response({"device_code": "DC", "user_code": "UC"})

        device = copilot.request_device_code(_client(handler))
        assert device.interval == 5
        assert device.expires_in == copilot.POLL_MAX_SECONDS

    def test_device_code_transport_error_is_fail_soft(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("no dns")

        with pytest.raises(copilot.CopilotError, match="cannot reach GitHub"):
            copilot.request_device_code(_client(handler))


class TestPolling:
    def test_poll_pending_then_success(self) -> None:
        responses = iter([
            _json_response({"error": "authorization_pending"}),
            _json_response({"error": "authorization_pending"}),
            _json_response({"access_token": "gho_OK"}),
        ])
        slept: list[float] = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = request.read().decode()
            assert "urn:ietf:params:oauth:grant-type:device_code" in body
            assert "DC123" in body
            return next(responses)

        token = copilot.poll_for_token(
            copilot.DeviceCode("DC123", "UC", "https://x", 1, 900),
            client=_client(handler),
            sleep=slept.append,
            deadline_seconds=60,
        )
        assert token == "gho_OK"
        assert slept == [1, 1, 1]

    def test_poll_slow_down_bumps_interval(self) -> None:
        """Server's slow_down increases the poll interval by 5s per spec."""
        responses = iter([
            _json_response({"error": "slow_down"}),
            _json_response({"access_token": "gho_OK"}),
        ])
        slept: list[float] = []

        def handler(request: httpx.Request) -> httpx.Response:
            return next(responses)

        copilot.poll_for_token(
            copilot.DeviceCode("DC", "UC", "https://x", 5, 900),
            client=_client(handler),
            sleep=slept.append,
            deadline_seconds=120,
        )
        assert slept == [5, 10]

    def test_poll_timeout_aborts_cleanly(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return _json_response({"error": "authorization_pending"})

        with pytest.raises(copilot.CopilotError, match="timed out"):
            copilot.poll_for_token(
                copilot.DeviceCode("DC", "UC", "https://x", 1, 900),
                client=_client(handler),
                sleep=lambda _s: None,
                deadline_seconds=0,
            )

    def test_poll_denied_aborts(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return _json_response({"error": "access_denied"})

        with pytest.raises(copilot.CopilotError, match="access_denied"):
            copilot.poll_for_token(
                copilot.DeviceCode("DC", "UC", "https://x", 1, 900),
                client=_client(handler),
                sleep=lambda _s: None,
            )

    def test_poll_capped_at_global_max(self) -> None:
        """A server claiming expires_in=999999 is still bounded to POLL_MAX_SECONDS.

        poll_for_token min()es the deadline against POLL_MAX_SECONDS, so an
        absurd expires_in can never stretch the wait past ~15 minutes.
        """
        assert copilot.POLL_MAX_SECONDS == 900


class TestEntitlement:
    def test_blocked_when_chat_disabled_and_signup_possible(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            auth = request.headers.get("Authorization")
            assert auth == "Bearer gho_X"
            return _json_response({"chat_enabled": False, "can_signup_for_limited": True})

        with pytest.raises(copilot.CopilotError, match="Copilot Free"):
            copilot.check_entitlement("gho_X", _client(handler))

    def test_soft_pass_on_allowed(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return _json_response({"chat_enabled": True})

        copilot.check_entitlement("gho_X", _client(handler))  # no raise

    def test_soft_pass_on_malformed_lookup(self) -> None:
        """A failed/malformed lookup must NOT deny login."""
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="boom")

        copilot.check_entitlement("gho_X", _client(handler))  # no raise

    def test_soft_pass_on_transport_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("down")

        copilot.check_entitlement("gho_X", _client(handler))  # no raise

    def test_soft_pass_on_non_dict_body(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=[1, 2])

        copilot.check_entitlement("gho_X", _client(handler))  # no raise


class TestTokenExchange:
    def test_exchange_parses_token_and_api_base(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url == httpx.URL(copilot.TOKEN_EXCHANGE_URI)
            assert request.headers["Authorization"] == "Bearer gho_X"
            return _json_response(
                {
                    "token": "tid=abc;exp=1",
                    "expires_at": 1737000000,
                    "endpoints": {"api": "https://api.business.githubcopilot.com"},
                }
            )

        result = copilot.exchange_token("gho_X", _client(handler))
        assert result.token == "tid=abc;exp=1"
        assert result.api_base == "https://api.business.githubcopilot.com"

    def test_exchange_falls_back_to_default_api_base(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return _json_response({"token": "tid=abc"})

        result = copilot.exchange_token("gho_X", _client(handler))
        assert result.token == "tid=abc"
        assert result.api_base == copilot.FALLBACK_API_BASE

    def test_exchange_missing_token_raises(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return _json_response({"error": "nope"})

        with pytest.raises(copilot.CopilotError, match="no token"):
            copilot.exchange_token("gho_X", _client(handler))


class TestPrepareLlmCall:
    def test_non_copilot_passes_through(self) -> None:
        llm_config = LlmConfig(base_url="https://api.openai.com/v1", api_key="sk-1")
        base_url, api_key, headers = copilot.prepare_llm_call(llm_config, None)
        assert base_url == "https://api.openai.com/v1"
        assert api_key == "sk-1"
        assert headers == {}

    def test_copilot_exchanges_and_attaches_headers(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return _json_response(
                {"token": "tid=short", "endpoints": {"api": "https://api.e.githubcopilot.com"}}
            )

        llm_config = LlmConfig(provider="copilot")
        base_url, api_key, headers = copilot.prepare_llm_call(
            llm_config, "gho_X", _client(handler)
        )
        assert base_url == "https://api.e.githubcopilot.com"
        assert api_key == "tid=short"
        assert headers["Openai-Intent"] == "conversation-edits"
        assert headers["x-initiator"] == "user"
        assert headers["X-GitHub-Api-Version"] == "2022-11-28"
        assert headers["User-Agent"].startswith("spiritcuffs/")

    def test_copilot_without_token_raises_cleanly(self) -> None:
        llm_config = LlmConfig(provider="copilot")
        with pytest.raises(copilot.CopilotError, match="spc setup"):
            copilot.prepare_llm_call(llm_config, None)


class TestLogin:
    def test_login_happy_path_prints_url_and_code(self) -> None:
        state = {"phase": 0}
        printed: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/login/device/code":
                return _json_response(
                    {
                        "device_code": "DC",
                        "user_code": "ABCD-1234",
                        "verification_uri": "https://github.com/login/device",
                        "interval": 1,
                    }
                )
            if request.url.path == "/copilot_internal/user":
                return _json_response({"chat_enabled": True})
            state["phase"] += 1
            if state["phase"] < 2:
                return _json_response({"error": "authorization_pending"})
            return _json_response({"access_token": "gho_NEW"})

        token = copilot.login(printed.append, sleep=lambda _s: None, client=_client(handler))
        assert token == "gho_NEW"
        joined = "\n".join(printed)
        assert "https://github.com/login/device" in joined
        assert "ABCD-1234" in joined

    def test_login_blocked_entitlement_propagates(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/login/device/code":
                return _json_response(
                    {"device_code": "DC", "user_code": "UC", "interval": 1}
                )
            if request.url.path == "/copilot_internal/user":
                return _json_response({"chat_enabled": False, "can_signup_for_limited": True})
            return _json_response({"access_token": "gho_X"})

        with pytest.raises(copilot.CopilotError, match="Copilot Free"):
            copilot.login(lambda _line: None, sleep=lambda _s: None, client=_client(handler))


class TestResolveCopilotToken:
    def test_repo_wins_over_user_and_env(
        self, user_config: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        save_user_config(user_config_path(), copilot_github_token="gho_user")
        monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "gho_env")
        assert resolve_copilot_token("gho_repo") == "gho_repo"

    def test_user_config_beats_env(
        self, user_config: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        save_user_config(user_config_path(), copilot_github_token="gho_user")
        monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "gho_env")
        assert resolve_copilot_token("") == "gho_user"

    def test_env_fallback(self, user_config: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "gho_env")
        assert resolve_copilot_token(None) == "gho_env"

    def test_none_when_nothing_set(self, user_config: Path) -> None:
        assert resolve_copilot_token(None) is None

    def test_repo_config_section_loads(self, tmp_path: Path) -> None:
        (tmp_path / ".spc.toml").write_text(
            '[copilot]\ngithub_oauth_token = "gho_repo"\n', encoding="utf-8"
        )
        config = load_config(tmp_path)
        assert isinstance(config.copilot, CopilotConfig)
        assert config.copilot.github_oauth_token == "gho_repo"


class TestWizardCopilot:
    def test_provider_list_has_copilot_sixth(self) -> None:
        keys = [p.key for p in PROVIDERS]
        assert keys == ["openrouter", "openai", "anthropic", "copilot", "ollama"][:4] + ["ollama"] or "copilot" in keys
        copilot_provider = next(p for p in PROVIDERS if p.key == "copilot")
        assert copilot_provider.kind == "copilot"
        assert copilot_provider.model == "gpt-4.1-mini"

    def test_non_interactive_without_token_errors_cleanly(
        self, user_config: Path
    ) -> None:
        """--provider copilot with no token: one-line hint, exit 0, nothing stored."""
        console = _console()
        exit_code = run_setup(
            console, non_interactive=True, provider_key="copilot", skip_ping=True
        )
        printed = console.file.getvalue()  # type: ignore[union-attr]
        assert exit_code == 0
        assert "--copilot-token" in printed
        assert load_user_config().copilot_github_token is None

    def test_copilot_token_flag_persists_mode_600(self, user_config: Path) -> None:
        console = _console()
        exit_code = run_setup(
            console,
            non_interactive=True,
            provider_key="copilot",
            copilot_token="gho_ABC123456789",
            skip_ping=True,
        )
        assert exit_code == 0
        path = user_config_path()
        config = load_user_config()
        assert config.copilot_github_token == "gho_ABC123456789"
        assert config.llm_provider == "copilot"
        assert config.llm_model == "gpt-4.1-mini"
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        # full token never echoed to console
        printed = console.file.getvalue()  # type: ignore[union-attr]
        assert "gho_ABC123456789" not in printed

    def test_interactive_device_flow_stores_token(
        self, user_config: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Full interactive path with the device flow faked end-to-end."""
        def fake_login(console_print, sleep=lambda _s: None, client=None):
            console_print("[bold]Open https://github.com/login/device and enter code ABCD-1234")
            return "gho_DEVICE"

        monkeypatch.setattr("spc.copilot.login", fake_login)
        console = _console()
        exit_code = run_setup(
            console,
            non_interactive=False,
            provider_key="copilot",
            skip_ping=True,
        )
        assert exit_code == 0
        config = load_user_config()
        assert config.copilot_github_token == "gho_DEVICE"
        assert config.llm_provider == "copilot"

    def test_interactive_login_failure_is_fail_soft(
        self, user_config: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fake_login(console_print, sleep=lambda _s: None, client=None):
            raise copilot.CopilotError("Copilot login timed out — run `spc setup` again")

        monkeypatch.setattr("spc.copilot.login", fake_login)
        console = _console()
        exit_code = run_setup(
            console,
            non_interactive=False,
            provider_key="copilot",
            skip_ping=True,
        )
        assert exit_code == 0
        printed = console.file.getvalue()  # type: ignore[union-attr]
        assert "Copilot login failed" in printed


class TestCopilotReviewPath:
    def test_review_uses_exchanged_token_and_headers(
        self, tmp_path: Path, user_config: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """End-to-end review-path plumbing: exchange → headers → base URL."""
        from spc.config import SpcConfig
        from spc.diff import DiffResult
        from spc.review import ReviewOutput, run_review

        save_user_config(user_config_path(), copilot_github_token="gho_RUN")
        monkeypatch.delenv("COPILOT_GITHUB_TOKEN", raising=False)
        captured: dict[str, object] = {}

        def fake_exchange(oauth_token, client=None):
            captured["oauth"] = oauth_token
            return copilot.CopilotToken(token="tid=RUN", api_base="https://api.run.githubcopilot.com")

        monkeypatch.setattr(copilot, "exchange_token", fake_exchange)

        def fake_complete(messages, *, model, base_url, api_key, timeout, **kwargs):  # noqa: ANN001, ANN003
            captured["base_url"] = base_url
            captured["api_key"] = api_key
            captured["extra_headers"] = kwargs.get("extra_headers")
            return '[{"severity": "warn", "file": "a.py", "line": 1, "message": "m", "suggestion": ""}]'

        monkeypatch.setattr("spc.review.llm.complete", fake_complete)
        config = SpcConfig(llm=LlmConfig(provider="copilot", model="gpt-4.1-mini"))
        output = run_review(
            DiffResult(text="", source="upstream", files=[]),
            profile=None,
            conventions=[],
            config=config,
            console=_console(),
        )
        assert isinstance(output, ReviewOutput)
        assert captured["oauth"] == "gho_RUN"
        assert captured["base_url"] == "https://api.run.githubcopilot.com"
        assert captured["api_key"] == "tid=RUN"
        headers = captured["extra_headers"]
        assert headers["Openai-Intent"] == "conversation-edits"
        assert headers["x-initiator"] == "user"

    def test_review_fail_soft_when_no_copilot_token(
        self, tmp_path: Path, user_config: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No OAuth token: one-line warning, empty findings, never raises."""
        from spc.config import SpcConfig
        from spc.diff import DiffResult
        from spc.review import run_review

        monkeypatch.delenv("COPILOT_GITHUB_TOKEN", raising=False)
        console = _console()
        output = run_review(
            DiffResult(text="", source="upstream", files=[]),
            profile=None,
            conventions=[],
            config=SpcConfig(llm=LlmConfig(provider="copilot")),
            console=console,
        )
        assert output.findings == []
        printed = console.file.getvalue()  # type: ignore[union-attr]
        assert "LLM review failed" in printed

    def test_copilot_error_is_never_echoed_with_token(self) -> None:
        """_redact covers both tokens in error text (llm-level guarantee)."""
        text = "HTTP 401: bad token tid=SECRET gho_X"
        assert llm._redact(text, "tid=SECRET") == "HTTP 401: bad token *** gho_X"
        assert llm._redact(text, "gho_X") == "HTTP 401: bad token tid=SECRET ***"
        # complete() redacts whatever api_key it is handed, copilot tokens included
        assert llm._redact("err tid=SHORT", "tid=SHORT") == "err ***"
