"""Source B — GitHub PR + inline review-comment mining (read-only REST).

Merged PRs and their review comments are reduced to *themes*: what peers
repeatedly flag. Raw evidence stays in ``.spc/evidence.json`` (gitignored);
only the LLM-synthesized theme section may enter ``.spc/profile.md``.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from spc import llm
from spc.config import SpcConfig, load_config
from spc.diff import run_git
from spc.githubapi import (
    DEFAULT_BASE_URL,
    FetchCaps,
    GithubApiError,
    GithubClient,
    RateLimitedError,
    parse_github_remote,
    remote_host,
    resolve_token,
    split_owner_repo_from_path,
)  # Caps on what enters the ONE theme-clustering LLM call.

MAX_PRS_IN_PROMPT = 100
MAX_COMMENTS_IN_PROMPT = 300
MAX_COMMENT_CHARS = 200
MAX_PROMPT_CHARS = 14000

REMOTE_TIMEOUT_SECONDS = 5.0


@dataclass
class PrRecord:
    """One merged PR, reduced to mining-relevant fields."""

    number: int
    title: str
    merged_at: str
    additions: int = 0
    deletions: int = 0
    changed_files: int = 0
    labels: list[str] = field(default_factory=list)


@dataclass
class CommentRecord:
    """One inline review comment, truncated to its first line(s)."""

    pr_number: int
    path: str
    body_snippet: str


@dataclass
class GithubEvidence:
    """Raw GitHub evidence for ``.spc/evidence.json`` (never enters the profile)."""

    owner: str
    repo: str
    mined_at: float = 0.0
    watermark: int = 0  # highest PR number seen; re-runs fetch only newer
    pulls: list[PrRecord] = field(default_factory=list)
    comments: list[CommentRecord] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe dict for evidence.json."""
        return {
            "owner": self.owner,
            "repo": self.repo,
            "mined_at": self.mined_at,
            "watermark": self.watermark,
            "pulls": [
                {
                    "number": p.number,
                    "title": p.title,
                    "merged_at": p.merged_at,
                    "additions": p.additions,
                    "deletions": p.deletions,
                    "changed_files": p.changed_files,
                    "labels": p.labels,
                }
                for p in self.pulls
            ],
            "comments": [
                {
                    "pr_number": c.pr_number,
                    "path": c.path,
                    "body_snippet": c.body_snippet,
                }
                for c in self.comments
            ],
            "stats": self.stats,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GithubEvidence:
        """Rehydrate from evidence.json; tolerates missing/mistyped fields."""
        pulls = [
            PrRecord(
                number=int(p.get("number", 0)),
                title=str(p.get("title", "")),
                merged_at=str(p.get("merged_at", "")),
                additions=int(p.get("additions", 0)),
                deletions=int(p.get("deletions", 0)),
                changed_files=int(p.get("changed_files", 0)),
                labels=[str(lb) for lb in p.get("labels", [])],
            )
            for p in data.get("pulls", [])
            if isinstance(p, dict)
        ]
        comments = [
            CommentRecord(
                pr_number=int(c.get("pr_number", 0)),
                path=str(c.get("path", "")),
                body_snippet=str(c.get("body_snippet", "")),
            )
            for c in data.get("comments", [])
            if isinstance(c, dict)
        ]
        stats = data.get("stats") if isinstance(data.get("stats"), dict) else {}
        return cls(
            owner=str(data.get("owner", "")),
            repo=str(data.get("repo", "")),
            mined_at=float(data.get("mined_at", 0.0)),
            watermark=int(data.get("watermark", 0)),
            pulls=pulls,
            comments=comments,
            stats=stats,
        )


def detect_github_repo(repo_root: Path) -> tuple[str, str] | None:
    """Parse the ``origin`` remote into ``(owner, repo)``; None for non-github.

    Accepts github.com URLs and GitHub-Enterprise-shaped URLs on any host —
    an origin the user themselves pointed at a GitHub-like API host. A
    non-GitHub URL shape (e.g. ``gitlab.com``) is rejected.
    """
    raw = run_git(repo_root, "remote", "get-url", "origin")
    if not raw:
        return None
    url = raw.strip()
    parsed = parse_github_remote(url)
    if parsed is not None:
        return parsed
    # GHE: GitHub-style path on a custom host that matches github.base_url's
    # host. Path shape must still be GitHub-like (owner/repo).
    base_host = remote_host(str(load_config(repo_root).github.base_url))
    origin_host = remote_host(url)
    if (
        origin_host
        and base_host
        and origin_host == base_host
        and base_host != "github.com"
    ):
        return split_owner_repo_from_path(url)
    return None


def decide_mining(config: SpcConfig) -> str:
    """Resolve ``github.enabled`` to "off" | "mine".

    "off": enabled=false — never touch the network. "mine": everything else
    (auto mode always attempts — unauthenticated for public repos, failing
    soft; explicit true forces mining).
    """
    enabled = str(config.github.enabled).strip().lower()
    if enabled in ("false", "no", "0"):
        return "off"
    return "mine"


def mine_github(
    repo_root: Path,
    config: SpcConfig,
    *,
    previous: GithubEvidence | None = None,
    client_factory: Any = None,
) -> GithubEvidence | None:
    """Fetch merged PRs + review comments into :class:`GithubEvidence`.

    Watermark-incremental: when *previous* evidence exists for the same repo,
    only PRs newer than its watermark are fetched and merged into it. Returns
    None when there is nothing to mine (no remote, disabled, no new PRs, or
    any API failure — the caller prints the reason). Rate-limit and API-budget
    exhaustion stop gracefully, keeping what was fetched.
    """
    gh_repo = detect_github_repo(repo_root)
    if gh_repo is None:
        return None
    owner, repo_name = gh_repo

    caps = FetchCaps(
        max_prs=config.github.github_max_prs,
        max_comments=config.github.github_max_comments,
        pages_max=config.github.github_pages_max,
        base_url=(
            str(config.github.base_url)
            if getattr(config.github, "base_url", None)
            else DEFAULT_BASE_URL
        ),
    )
    # Trust anchor for token auth: the host the user themselves pointed
    # origin at (their GHE instance) — never the repo-committed base_url.
    origin_url = run_git(repo_root, "remote", "get-url", "origin") or ""
    origin_host = remote_host(origin_url.strip())

    try:
        if client_factory is not None:
            client = client_factory(caps)
        else:
            client = GithubClient(resolve_token(), caps, origin_host=origin_host)
        try:
            return _mine_with_client(client, owner, repo_name, previous)
        except GithubApiError:
            # Rate-limit/budget exhaustion is handled inside _mine_with_client
            # (graceful stop, keep fetched work); anything else discards the
            # run — fail-soft.
            return None
        finally:
            client.close()
    except GithubApiError:
        return None


def _mine_with_client(
    client: GithubClient,
    owner: str,
    repo_name: str,
    previous: GithubEvidence | None,
) -> GithubEvidence | None:
    """Drive the client through PR pages and comment endpoints."""
    reused = (
        previous
        if (previous and previous.owner == owner and previous.repo == repo_name)
        else None
    )
    watermark = reused.watermark if reused else 0

    evidence = GithubEvidence(
        owner=owner,
        repo=repo_name,
        mined_at=time.time(),
        watermark=watermark,
        pulls=list(reused.pulls) if reused else [],
        comments=list(reused.comments) if reused else [],
    )

    new_prs: list[PrRecord] = []
    new_comments: list[CommentRecord] = []
    try:
        for pull in client.iter_merged_pulls(owner, repo_name, min_number=watermark):
            number = int(pull.get("number", 0))
            record = PrRecord(
                number=number,
                title=str(pull.get("title", ""))[:300],
                merged_at=str(pull.get("merged_at", "")),
                additions=int(pull.get("additions", 0) or 0),
                deletions=int(pull.get("deletions", 0) or 0),
                changed_files=int(pull.get("changed_files", 0) or 0),
                labels=[
                    str(lb.get("name", "")) for lb in pull.get("labels", []) if isinstance(lb, dict)
                ],
            )
            new_prs.append(record)
            for comment in client.fetch_review_comments(owner, repo_name, number):
                new_comments.append(
                    CommentRecord(
                        pr_number=number,
                        path=str(comment.get("path", "")),
                        body_snippet=str(comment.get("body", ""))[:MAX_COMMENT_CHARS * 2],
                    )
                )
    except RateLimitedError:
        pass  # keep everything fetched so far; stats.stopped_reason already says why

    if not new_prs:
        return None  # nothing new — previous evidence already covers the repo

    evidence.pulls.extend(new_prs)
    evidence.comments.extend(new_comments)
    evidence.watermark = max([watermark, *(p.number for p in new_prs)])
    evidence.stats = {
        "prs_fetched": client.stats.prs_fetched,
        "comments_fetched": client.stats.comments_fetched,
        "api_calls": client.stats.api_calls,
        "pages_fetched": client.stats.pages_fetched,
        "stopped_reason": client.stats.stopped_reason,
        "rate_limit_remaining": client.stats.rate_limit_remaining,
        "fetched_at": time.time(),
    }
    # Keep merged evidence newest-first across incremental runs.
    evidence.pulls.sort(key=lambda record: record.number, reverse=True)
    return evidence


def cluster_themes(evidence: GithubEvidence, config: SpcConfig) -> str | None:
    """Cluster recurring review themes via one LLM pass; None on any failure."""
    if not evidence.pulls and not evidence.comments:
        return None
    try:
        api_key = llm.resolve_api_key(config.llm.api_key_env)
        return llm.complete(
            [
                {"role": "system", "content": _CLUSTER_SYSTEM},
                {"role": "user", "content": _render_cluster_prompt(evidence)},
            ],
            model=config.llm.model,
            base_url=config.llm.base_url,
            api_key=api_key,
            timeout=config.llm.timeout,
        )
    except Exception:  # noqa: BLE001 — theme clustering is best-effort by design
        return None


_CLUSTER_SYSTEM = (
    "You are a senior engineer distilling a repository's code-review history. "
    "Input: merged PR titles and inline review comments from peers. Output: a "
    "short markdown section listing the recurring review themes (what reviewers "
    "repeatedly ask for), each with a brief count of how often it appeared. "
    "Rules: synthesize — never quote comments verbatim, never mention authors, "
    "never include code snippets or secrets. Keep it under 200 words. If the "
    "evidence is too thin to find themes, say so in one sentence."
)


def _render_cluster_prompt(evidence: GithubEvidence) -> str:
    """Render capped PR/comment evidence for the clustering call."""
    pulls = evidence.pulls[:MAX_PRS_IN_PROMPT]
    comments = evidence.comments[:MAX_COMMENTS_IN_PROMPT]

    lines = ["Merged PR titles (newest first):"]
    for pull in pulls:
        lines.append(
            f"- #{pull.number}: {pull.title}"
            f" (+{pull.additions}/-{pull.deletions}, {pull.changed_files} files)"
        )

    if comments:
        lines.append("")
        lines.append("Inline review comment snippets:")
        for comment in comments:
            path = comment.path or "(general)"
            snippet = comment.body_snippet.replace("\n", " ")[:MAX_COMMENT_CHARS]
            lines.append(f"- [#{comment.pr_number} {path}] {snippet}")

    text = "\n".join(lines)
    if len(text) > MAX_PROMPT_CHARS:
        text = text[:MAX_PROMPT_CHARS] + "\n…(truncated)"
    return text
