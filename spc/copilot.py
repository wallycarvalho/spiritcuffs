"""GitHub Copilot as an LLM provider: device-flow login and per-run token exchange.

Replicates the official GitHub partnership flow (as integrated by OpenCode,
partnership announced 2026-01-15):

1. One-time interactive OAuth Device Flow → long-lived GitHub OAuth token.
2. Soft entitlement check (only an explicit "chat disabled" answer blocks).
3. Per-run exchange of the OAuth token for a short-lived (~30 min) Copilot
   API token, which is never persisted to disk.

GitHub Enterprise is explicitly not supported yet (the device flow and
``copilot_internal`` endpoints are github.com-only).
"""

from __future__ import annotations

import importlib.metadata
import time
from collections.abc import Callable
from dataclasses import dataclass

import httpx

from spc.config import LlmConfig


def _user_agent() -> str:
    """``spiritcuffs/<version>``; dev fallback when the package isn't installed."""
    try:
        return f"spiritcuffs/{importlib.metadata.version('spiritcuffs')}"
    except importlib.metadata.PackageNotFoundError:
        return "spiritcuffs/dev"


USER_AGENT = _user_agent()

CLIENT_ID = "Ov23li5Rr1rgjMTpz5am"
DEVICE_CODE_URI = "https://github.com/login/device/code"
TOKEN_URI = "https://github.com/login/oauth/access_token"
ENTITLEMENT_URI = "https://api.github.com/copilot_internal/user"
TOKEN_EXCHANGE_URI = "https://api.github.com/copilot_internal/v2/token"
FALLBACK_API_BASE = "https://api.githubcopilot.com"
# Device codes expire in ~15 minutes; polling must give up before that.
POLL_MAX_SECONDS = 900
GITHUB_API_VERSION = "2022-11-28"

# Headers Copilot's endpoint requires; without them requests are rejected.
COPILOT_CHAT_HEADERS = {
    "Openai-Intent": "conversation-edits",
    "x-initiator": "user",
}


class CopilotError(RuntimeError):
    """Raised when the Copilot device flow or token exchange fails."""


@dataclass(frozen=True)
class DeviceCode:
    """The pending device-flow session shown to the user."""

    device_code: str
    user_code: str
    verification_uri: str
    interval: int
    expires_in: int


@dataclass(frozen=True)
class CopilotToken:
    """Short-lived Copilot API token plus the base URL to call with it."""

    token: str
    api_base: str


def _headers() -> dict[str, str]:
    """Headers shared by the github.com OAuth calls."""
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
    }


def request_device_code(client: httpx.Client | None = None) -> DeviceCode:
    """Start the OAuth device flow; returns the code/URL the user must visit."""
    response = _post(
        DEVICE_CODE_URI,
        json={"client_id": CLIENT_ID, "scope": "read:user"},
        client=client,
    )
    body = _json(response)
    device_code = body.get("device_code")
    user_code = body.get("user_code")
    verification_uri = body.get("verification_uri")
    if not isinstance(device_code, str) or not isinstance(user_code, str):
        raise CopilotError("device-code response missing fields — try again later")
    interval = _positive_int(body.get("interval"), default=5)
    expires_in = _positive_int(body.get("expires_in"), default=POLL_MAX_SECONDS)
    return DeviceCode(
        device_code=device_code,
        user_code=user_code,
        verification_uri=(
            verification_uri if isinstance(verification_uri, str) else "https://github.com/login/device"
        ),
        interval=interval,
        expires_in=expires_in,
    )


def poll_for_token(
    device: DeviceCode,
    *,
    client: httpx.Client | None = None,
    sleep: Callable[[float], None] = time.sleep,
    deadline_seconds: int | None = None,
    now: Callable[[], float] = time.monotonic,
) -> str:
    """Poll until the user approves, giving up after ~15 minutes.

    Honors ``interval`` and the server's ``slow_down`` bump; raises
    :class:`CopilotError` with a clean message on expiry or access denial.

    The deadline is wall-clock (injectable ``now``, default
    ``time.monotonic``), not accumulated sleep: a hung poll attempt (each has
    its own transport timeout) must still end inside the overall bound.
    """
    bound = deadline_seconds if deadline_seconds is not None else device.expires_in
    bound = min(bound, POLL_MAX_SECONDS)
    deadline = now() + bound
    interval = device.interval
    while True:
        if now() >= deadline:
            raise CopilotError(
                "Copilot login timed out — run `spc setup` again when ready "
                f"(gave up after {bound}s)"
            )
        sleep(interval)
        response = _post(
            TOKEN_URI,
            json={
                "client_id": CLIENT_ID,
                "device_code": device.device_code,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            },
            client=client,
        )
        body = _json(response)
        error = body.get("error")
        if error == "authorization_pending":
            continue
        if error == "slow_down":
            interval += 5
            continue
        if error == "expired_token":
            raise CopilotError("Copilot login expired — run `spc setup` to retry")
        if error:
            raise CopilotError(f"Copilot login failed: {error}")
        access_token = body.get("access_token")
        if isinstance(access_token, str) and access_token:
            return access_token
        raise CopilotError("Copilot login response missing access_token")


def check_entitlement(oauth_token: str, client: httpx.Client | None = None) -> None:
    """Soft entitlement check: only an explicit "chat disabled" answer blocks.

    A failed or malformed lookup must NOT deny login (matching OpenCode's
    rule) — network errors and odd payloads are silently tolerated here.
    """
    client = client or httpx.Client(timeout=15.0)
    try:
        response = client.get(
            ENTITLEMENT_URI,
            headers={
                "Authorization": f"Bearer {oauth_token}",
                "X-GitHub-Api-Version": GITHUB_API_VERSION,
                "User-Agent": USER_AGENT,
            },
        )
        body = _json(response)
    except (httpx.HTTPError, ValueError):
        return
    chat_enabled = body.get("chat_enabled")
    can_signup = body.get("can_signup_for_limited")
    if chat_enabled is False and can_signup is True:
        raise CopilotError(
            "Copilot chat is not enabled on your account — "
            "sign up for Copilot Free at https://github.com/features/copilot"
        )


def exchange_token(
    oauth_token: str, client: httpx.Client | None = None
) -> CopilotToken:
    """Trade the long-lived OAuth token for a short-lived Copilot API token.

    The Copilot token must never be persisted — it lives for one run only.
    """
    response = _post_authed(
        TOKEN_EXCHANGE_URI, oauth_token, client=client
    )
    body = _json(response)
    token = body.get("token")
    if not isinstance(token, str) or not token:
        raise CopilotError("Copilot token exchange returned no token — try again")
    endpoints = body.get("endpoints") if isinstance(body.get("endpoints"), dict) else {}
    api_base = endpoints.get("api") if isinstance(endpoints.get("api"), str) else None
    return CopilotToken(
        token=token,
        api_base=api_base if api_base else FALLBACK_API_BASE,
    )


def prepare_llm_call(
    llm: LlmConfig, copilot_oauth_token: str | None, client: httpx.Client | None = None
) -> tuple[str, str, dict[str, str]]:
    """Resolve (base_url, api_key, extra_headers) for a Copilot run.

    The OAuth token is exchanged for a short-lived Copilot token (never
    persisted) and the required Copilot headers are attached; the base URL
    comes from the exchange response's ``endpoints.api`` when present.
    Raises :class:`CopilotError` when there is no OAuth token — the caller
    fails soft.
    """
    if not copilot_oauth_token:
        raise CopilotError(
            "no Copilot OAuth token — run `spc setup` (choose GitHub Copilot) "
            "or set COPILOT_GITHUB_TOKEN"
        )
    exchanged = exchange_token(copilot_oauth_token, client)
    headers = {
        **COPILOT_CHAT_HEADERS,
        "User-Agent": USER_AGENT,
        "X-GitHub-Api-Version": GITHUB_API_VERSION,
    }
    return exchanged.api_base, exchanged.token, headers


def _post(url: str, json: dict[str, str], client: httpx.Client | None) -> httpx.Response:
    """POST via the injected client or a fresh one; raises CopilotError on transport errors."""
    try:
        if client is not None:
            return client.post(url, json=json, headers=_headers())
        return httpx.post(url, json=json, headers=_headers(), timeout=15.0)
    except httpx.HTTPError as exc:
        raise CopilotError(f"cannot reach GitHub ({type(exc).__name__}) — try again") from exc


def _post_authed(
    url: str, bearer: str, client: httpx.Client | None
) -> httpx.Response:
    """Authenticated POST (token exchange); transport errors become CopilotError."""
    headers = {**_headers(), "Authorization": f"Bearer {bearer}"}
    try:
        if client is not None:
            return client.post(url, json={}, headers=headers)
        return httpx.post(url, json={}, headers=headers, timeout=15.0)
    except httpx.HTTPError as exc:
        raise CopilotError(f"cannot reach GitHub ({type(exc).__name__}) — try again") from exc


def _json(response: httpx.Response) -> dict[str, object]:
    """Parse a JSON object body; malformed bodies yield {} (callers handle it)."""
    try:
        data = response.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _positive_int(value: object, *, default: int) -> int:
    """Coerce *value* to a positive int, or *default*."""
    if isinstance(value, int) and value > 0:
        return value
    return default


def login(
    console_print: callable,  # type: ignore[assignment]
    sleep: callable = time.sleep,  # type: ignore[assignment]
    client: httpx.Client | None = None,
) -> str:
    """Interactive device-flow login; returns the long-lived GitHub OAuth token.

    *console_print* receives the already-styled lines (keeps rich out of this
    module). Raises :class:`CopilotError` on timeout/denial — the caller fails
    soft.
    """
    device = request_device_code(client)
    console_print(
        f"[bold]Open [cyan]{device.verification_uri}[/] and enter code "
        f"[bold]{device.user_code}[/]"
    )
    console_print("[dim]waiting for approval (this can take a minute)…[/]")
    oauth_token = poll_for_token(device, client=client, sleep=sleep)
    check_entitlement(oauth_token, client)
    return oauth_token
