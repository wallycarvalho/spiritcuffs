"""Thin GitHub REST client (httpx): remote parsing, token resolution, capped pagination.

Read-only. No ``gh`` subprocess except the optional ``gh auth token`` probe.
All functions are fail-soft: they return None/empty and never raise.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import httpx

DEFAULT_BASE_URL = "https://api.github.com"
DEFAULT_HTTP_TIMEOUT = 15.0
# A first run must be bounded even on huge repos: hard ceiling on API calls.
MAX_API_CALLS_PER_RUN = 60


class GithubApiError(RuntimeError):
    """Raised by the client on HTTP/network failure after honoring backoff."""


class RateLimitedError(GithubApiError):
    """Rate budget exhausted — callers keep fetched work and stop gracefully."""


@dataclass(frozen=True)
class FetchCaps:
    """Knobs bounding one mining run (all from ``[github]`` config)."""

    max_prs: int = 200
    max_comments: int = 1000
    pages_max: int = 10
    base_url: str = DEFAULT_BASE_URL


@dataclass
class FetchStats:
    """Bookkeeping for one fetch session (kept inside evidence for transparency)."""

    prs_fetched: int = 0
    comments_fetched: int = 0
    api_calls: int = 0
    stopped_reason: str = "complete"  # complete | cap_prs | cap_comments | rate_limit | error
    pages_fetched: int = 0
    rate_limit_remaining: int | None = None


def parse_github_remote(remote_url: str) -> tuple[str, str] | None:
    """Extract ``(owner, repo)`` from an origin URL; None for non-github remotes.

    Handles https and ssh forms, optional ``.git`` suffix, and trailing slash:
        https://github.com/owner/repo(.git)
        git@github.com:owner/repo(.git)
        ssh://git@github.com/owner/repo(.git)
    """
    url = remote_url.strip()
    if not url:
        return None
    prefixes = ("https://github.com/", "git@github.com:", "ssh://git@github.com/")
    prefix = next((p for p in prefixes if url.startswith(p)), None)
    if prefix is None:
        return None

    path = url[len(prefix):].removesuffix(".git").rstrip("/")
    parts = [p for p in path.split("/") if p]
    if len(parts) < 2:
        return None
    owner, repo = parts[0], parts[1]
    if not owner or not repo:
        return None
    return owner, repo


def resolve_token() -> str | None:
    """Token resolution order: GITHUB_TOKEN → GH_TOKEN → `gh auth token` probe.

    The ``gh`` probe runs only when the binary exists on PATH; any failure is
    swallowed (fail-soft by design).
    """
    for env_name in ("GITHUB_TOKEN", "GH_TOKEN"):
        token = os.environ.get(env_name)
        if token:
            return token
    gh_binary = shutil.which("gh")
    if gh_binary is None:
        return None
    try:
        result = subprocess.run(
            [gh_binary, "auth", "token"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    token = result.stdout.strip()
    return token or None


class GithubClient:
    """Capped, rate-limit-aware GET-only GitHub REST client.

    Raises :class:`GithubApiError` on HTTP/network failure — the mining layer
    catches it and degrades to git-only evidence. Pagination stops at the
    caps (``FetchCaps``) and the per-run API-call ceiling.
    """

    def __init__(
        self,
        token: str | None,
        caps: FetchCaps,
        *,
        client: httpx.Client | None = None,
        sleep_fn: Any = time.sleep,
    ) -> None:
        self._token = token
        self._caps = caps
        self._client = client or httpx.Client(
            timeout=DEFAULT_HTTP_TIMEOUT,
            headers=self._default_headers(token),
        )
        self._owns_client = client is None
        self._sleep = sleep_fn
        self.stats = FetchStats()

    @staticmethod
    def _default_headers(token: str | None) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "spiritcuffs",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    def close(self) -> None:
        """Release the underlying httpx client when we own it."""
        if self._owns_client:
            self._client.close()

    def _get(self, url: str, params: dict[str, Any] | None = None) -> httpx.Response:
        """One GET with rate-limit tracking, 403/429 backoff, and call ceiling."""
        if self.stats.api_calls >= MAX_API_CALLS_PER_RUN:
            self.stats.stopped_reason = "error"
            raise GithubApiError("API call ceiling reached")
        self.stats.api_calls += 1
        try:
            response = self._client.get(url, params=params)
        except httpx.HTTPError as exc:
            self.stats.stopped_reason = "error"
            raise GithubApiError(f"network error: {exc}") from exc

        remaining = response.headers.get("X-RateLimit-Remaining")
        if remaining is not None and remaining.isdigit():
            self.stats.rate_limit_remaining = int(remaining)
            if int(remaining) == 0:
                self.stats.stopped_reason = "rate_limit"
                raise RateLimitedError("GitHub rate limit exhausted")

        if response.status_code in (403, 429):
            # 403 can be rate-limit OR forbidden; 429 is throttling. Either way
            # back off once, then surface the failure to the (fail-soft) caller.
            self._sleep(1.0)
        if response.is_error:
            self.stats.stopped_reason = "error"
            raise GithubApiError(
                f"HTTP {response.status_code}: {response.text[:200]}"
            )
        return response

    def iter_merged_pulls(
        self, owner: str, repo: str, *, min_number: int = 0
    ) -> Iterator[dict[str, Any]]:
        """Yield merged PRs newest-first, stopping at caps and the watermark.

        *min_number* is the stored watermark: PRs numbered at or below it were
        mined by a previous run and are skipped.
        """
        page = 1
        while page <= self._caps.pages_max:
            if self.stats.prs_fetched >= self._caps.max_prs:
                self.stats.stopped_reason = "cap_prs"
                return
            try:
                response = self._get(
                    f"{self._caps.base_url.rstrip('/')}/repos/{owner}/{repo}/pulls",
                    params={"state": "closed", "per_page": 50, "page": page,
                            "sort": "created", "direction": "desc"},
                )
            except RateLimitedError:
                return  # graceful stop — already-yielded PRs stay with the consumer
            self.stats.pages_fetched += 1
            try:
                batch = response.json()
            except ValueError as exc:
                raise GithubApiError(f"malformed JSON: {exc}") from exc
            if not isinstance(batch, list) or not batch:
                return
            for pull in batch:
                if not isinstance(pull, dict):
                    continue
                number = pull.get("number")
                if not isinstance(number, int):
                    continue
                if number <= min_number:
                    return  # older than the watermark — everything after is too
                if pull.get("merged_at") is None:
                    continue  # closed-but-unmerged PRs carry no review signal
                if self.stats.prs_fetched >= self._caps.max_prs:
                    self.stats.stopped_reason = "cap_prs"
                    return
                self.stats.prs_fetched += 1
                yield pull
            page += 1

    def fetch_review_comments(
        self, owner: str, repo: str, pull_number: int
    ) -> list[dict[str, Any]]:
        """Inline review comments for one PR, bounded by the global comment cap."""
        if self.stats.comments_fetched >= self._caps.max_comments:
            self.stats.stopped_reason = "cap_comments"
            return []
        try:
            response = self._get(
                f"{self._caps.base_url.rstrip('/')}/repos/{owner}/{repo}"
                f"/pulls/{pull_number}/comments",
                params={"per_page": 50},
            )
        except RateLimitedError:
            return []  # comment budget is spent; PR walk stops gracefully next page
        try:
            batch = response.json()
        except ValueError as exc:
            raise GithubApiError(f"malformed JSON: {exc}") from exc
        if not isinstance(batch, list):
            return []
        budget = self._caps.max_comments - self.stats.comments_fetched
        trimmed = [c for c in batch if isinstance(c, dict)][:budget]
        self.stats.comments_fetched += len(trimmed)
        if len(trimmed) < len([c for c in batch if isinstance(c, dict)]):
            self.stats.stopped_reason = "cap_comments"
        return trimmed
