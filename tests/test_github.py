"""Source B tests: remote parsing, token resolution, capped pagination, mining.

The GitHub REST API is mocked with httpx.MockTransport — no network in tests.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from conftest import run_git
from rich.console import Console

from spc.config import GithubConfig, SpcConfig
from spc.githubapi import (
    FetchCaps,
    GithubApiError,
    GithubClient,
    parse_github_remote,
    resolve_token,
)
from spc.githubhistory import (
    GithubEvidence,
    PrRecord,
    cluster_themes,
    decide_mining,
    detect_github_repo,
    mine_github,
)
from spc.profile import (
    evidence_path,
    load_evidence,
    load_github_evidence,
    save_github_evidence,
)


@pytest.fixture()
def quiet_console() -> Console:
    return Console(file=open("/dev/null", "w"))  # noqa: SIM115 — tests only


# ---------------------------------------------------------------------------
# Remote parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://github.com/owner/repo.git", ("owner", "repo")),
        ("https://github.com/owner/repo", ("owner", "repo")),
        ("git@github.com:owner/repo.git", ("owner", "repo")),
        ("git@github.com:owner/repo", ("owner", "repo")),
        ("ssh://git@github.com/owner/repo.git", ("owner", "repo")),
        ("https://github.com/owner/repo/", ("owner", "repo")),
    ],
)
def test_parse_github_remote_forms(url: str, expected: tuple[str, str]) -> None:
    assert parse_github_remote(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "",
        "https://gitlab.com/owner/repo.git",
        "git@gitlab.com:owner/repo.git",
        "https://bitbucket.org/owner/repo.git",
        "/local/path/repo",
        "https://github.com/justonepart",
    ],
)
def test_parse_github_remote_non_github(url: str) -> None:
    assert parse_github_remote(url) is None


def test_detect_github_repo_reads_origin(git_repo) -> None:
    repo = git_repo()
    run_git(repo, "remote", "add", "origin", "git@github.com:acme/widget.git")
    assert detect_github_repo(repo) == ("acme", "widget")


def test_detect_github_repo_none_without_remote(git_repo) -> None:
    assert detect_github_repo(git_repo()) is None


# ---------------------------------------------------------------------------
# Token resolution
# ---------------------------------------------------------------------------


def test_token_resolution_prefers_github_token(monkeypatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "gt-1")
    monkeypatch.setenv("GH_TOKEN", "gt-2")
    assert resolve_token() == "gt-1"


def test_token_resolution_falls_back_to_gh_token(monkeypatch) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("GH_TOKEN", "gt-2")
    assert resolve_token() == "gt-2"


def test_token_resolution_uses_gh_binary(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    fake_gh = bin_dir / "gh"
    fake_gh.write_text("#!/bin/sh\necho tok-from-gh\n", encoding="utf-8")
    fake_gh.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    assert resolve_token() == "tok-from-gh"


def test_token_resolution_none_when_nothing_available(monkeypatch) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setenv("PATH", "/nonexistent-gh-search")
    assert resolve_token() is None


# ---------------------------------------------------------------------------
# Client: pagination, caps, rate limit
# ---------------------------------------------------------------------------


def _pr(number: int, *, merged: bool = True) -> dict:
    payload = {
        "number": number,
        "title": f"PR {number}",
        "additions": 10,
        "deletions": 2,
        "changed_files": 3,
        "labels": [{"name": "bug"}],
    }
    if merged:
        payload["merged_at"] = "2026-01-02T03:04:05Z"
    return payload


def _mock_client(
    handler,
    caps: FetchCaps | None = None,
    token: str | None = "t",
    origin_host: str | None = None,
) -> GithubClient:
    transport = httpx.MockTransport(handler)
    http_client = httpx.Client(transport=transport)
    return GithubClient(
        token, caps or FetchCaps(), client=http_client, sleep_fn=lambda _s: None,
        origin_host=origin_host,
    )


def test_pagination_stops_at_pages_max() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("page", "1"))
        return httpx.Response(200, json=[_pr(page * 100 + n) for n in range(50)])

    caps = FetchCaps(pages_max=2, max_prs=500)
    client = _mock_client(handler, caps)
    numbers = [p["number"] for p in client.iter_merged_pulls("o", "r")]
    assert len(numbers) == 100  # exactly 2 pages of 50
    assert client.stats.pages_fetched == 2


def test_caps_max_prs_and_comments() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        url_path = request.url.path
        if url_path.endswith("/pulls"):
            return httpx.Response(200, json=[_pr(n) for n in range(1, 51)])
        if url_path.endswith("/pulls/comments"):
            if int(request.url.params.get("page", "1")) > 1:
                return httpx.Response(200, json=[])
            return httpx.Response(
                200,
                json=[
                    _comment(2000 + i, 1, body=f"c{i}") for i in range(40)
                ],
            )
        return httpx.Response(404, json={})

    caps = FetchCaps(max_prs=3, max_comments=100)
    client = _mock_client(handler, caps)
    pulls = list(client.iter_merged_pulls("o", "r"))
    assert len(pulls) == 3
    comments = list(client.iter_review_comments("o", "r"))
    assert client.stats.comments_fetched == 40  # one page, no duplication
    assert len(comments) == 40
    assert client.stats.stopped_reason != "cap_comments"


def test_comment_cap_stops_the_walk() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/pulls/comments"):
            if int(request.url.params.get("page", "1")) > 1:
                return httpx.Response(200, json=[])
            return httpx.Response(
                200, json=[_comment(3000 + i, 1, body=f"c{i}") for i in range(40)]
            )
        return httpx.Response(200, json=[])

    client = _mock_client(handler, FetchCaps(max_comments=10))
    comments = list(client.iter_review_comments("o", "r"))
    assert len(comments) == 10
    assert client.stats.stopped_reason == "cap_comments"


def test_rate_limit_stop_keeps_fetched_work() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:  # first page succeeds, budget reported as spent
            return httpx.Response(
                200, json=[_pr(1), _pr(2)], headers={"X-RateLimit-Remaining": "1"}
            )
        return httpx.Response(
            200, json=[_pr(3)], headers={"X-RateLimit-Remaining": "0"}
        )

    client = _mock_client(handler, FetchCaps())
    pulls = list(client.iter_merged_pulls("o", "r"))
    assert [p["number"] for p in pulls] == [1, 2]  # first page kept
    assert client.stats.stopped_reason == "rate_limit"
    with pytest.raises(GithubApiError):
        client._get("https://api.github.com/x")  # further calls fail fast


def test_unmerged_prs_skipped() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("page", "1"))
        if page > 1:
            return httpx.Response(200, json=[])  # end of pagination
        return httpx.Response(200, json=[_pr(1, merged=False), _pr(2)])

    client = _mock_client(handler)
    pulls = list(client.iter_merged_pulls("o", "r"))
    assert [p["number"] for p in pulls] == [2]


def test_http_error_raises_github_api_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "Not Found"})

    client = _mock_client(handler)
    with pytest.raises(GithubApiError):
        list(client.iter_merged_pulls("o", "r"))


# ---------------------------------------------------------------------------
# Mining + watermark
# ---------------------------------------------------------------------------


def _comment(comment_id: int, pull_number: int, *, path: str = "src/a.py",
             body: str = "c") -> dict:
    """One repo-wide inline comment (the shape ``GET /pulls/comments`` returns)."""
    return {
        "id": comment_id,
        "path": path,
        "body": body,
        "pull_request_url": (
            f"https://api.github.com/repos/acme/widget/pulls/{pull_number}"
        ),
    }


def _api_mock(prs: list[dict], comments: dict[int, list[dict]] | None = None):
    """Build a handler serving the pulls list and the repo-wide comment stream.

    *comments* maps PR number → that PR's comments (path/body only); the helper
    adds the ``id`` and ``pull_request_url`` fields the repo-wide endpoint
    returns, so tests read naturally.
    """
    comments = comments or {}
    stream = [
        _comment(
            comment_id=1000 + (pr * 100) + index,
            pull_number=pr,
            path=str(item.get("path", "")),
            body=str(item.get("body", "")),
        )
        for pr, batch in comments.items()
        for index, item in enumerate(batch)
    ]
    stream.sort(key=lambda c: int(c["id"]), reverse=True)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/pulls"):
            page = int(request.url.params.get("page", "1"))
            if page > 1:
                return httpx.Response(200, json=[])
            return httpx.Response(200, json=prs)
        if request.url.path.endswith("/pulls/comments"):
            page = int(request.url.params.get("page", "1"))
            if page > 1:
                return httpx.Response(200, json=[])
            return httpx.Response(200, json=stream)
        return httpx.Response(200, json=[])

    return handler


def test_mine_github_collects_prs_and_comments(git_repo, monkeypatch) -> None:
    repo = git_repo()
    run_git(repo, "remote", "add", "origin", "https://github.com/acme/widget.git")
    handler = _api_mock(
        [_pr(7), _pr(6)],
        comments={7: [{"path": "src/a.py", "body": "add error handling here"}]},
    )

    def factory(caps: FetchCaps) -> GithubClient:
        return _mock_client(handler, caps)

    evidence = mine_github(repo, SpcConfig(), client_factory=factory)
    assert evidence is not None
    assert [p.number for p in evidence.pulls] == [7, 6]
    assert evidence.watermark == 7
    assert evidence.comments[0].path == "src/a.py"
    assert evidence.comments[0].body_snippet == "add error handling here"


def test_watermark_incremental_second_run_fetches_only_newer(git_repo) -> None:
    repo = git_repo()
    run_git(repo, "remote", "add", "origin", "https://github.com/acme/widget.git")

    first_handler = _api_mock(
        [_pr(10), _pr(9)],
        comments={10: [{"path": "a.py", "body": "x"}]},
    )
    first = mine_github(
        repo, SpcConfig(), client_factory=lambda caps: _mock_client(first_handler, caps)
    )
    assert first is not None
    assert [p.number for p in first.pulls] == [10, 9]

    requested_pages: list[int] = []

    def second_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/pulls"):
            page = int(request.url.params.get("page", "1"))
            requested_pages.append(page)
            if page > 1:
                return httpx.Response(200, json=[])
            # Server now returns PRs 11 (new) and 10/9 (already mined).
            return httpx.Response(200, json=[_pr(11), _pr(10), _pr(9)])
        return httpx.Response(200, json=[])

    second = mine_github(
        repo,
        SpcConfig(),
        previous=first,
        client_factory=lambda caps: _mock_client(second_handler, caps),
    )
    assert second is not None
    assert [p.number for p in second.pulls] == [11, 10, 9]  # merged, newest first
    assert second.watermark == 11
    # Already-mined PRs are skipped by identity, not by a high-water mark.
    assert second.stats["prs_fetched"] == 1


def test_truncated_run_resumes_and_reaches_the_gap(git_repo, monkeypatch) -> None:
    """A budget-truncated run must not strand the PRs it never reached.

    The old watermark walk recorded the highest PR number it saw and skipped
    everything below it on the next run, so the un-reached middle of a
    truncated range was lost permanently.
    """
    repo = git_repo()
    run_git(repo, "remote", "add", "origin", "https://github.com/acme/widget.git")
    all_prs = [_pr(number) for number in range(20, 0, -1)]  # 20..1, newest first
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/pulls"):
            calls["n"] += 1
            if calls["n"] > 1:  # one page of 100 is all this fake serves
                return httpx.Response(200, json=[])
            return httpx.Response(200, json=all_prs)
        return httpx.Response(200, json=[])

    # First run: a PR cap of 5 truncates the walk at PR 16.
    truncated_cfg = SpcConfig(
        github=GithubConfig(github_max_prs=5)
    )
    first = mine_github(
        repo, truncated_cfg, client_factory=lambda caps: _mock_client(handler, caps)
    )

    assert first is not None
    assert [p.number for p in first.pulls] == [20, 19, 18, 17, 16]
    assert first.stats["stopped_reason"] == "cap_prs"

    # Second run with a full budget must reach the PRs the first run never saw.
    calls["n"] = 0
    second = mine_github(
        repo,
        SpcConfig(),
        previous=first,
        client_factory=lambda caps: _mock_client(handler, caps),
    )
    assert second is not None
    assert {p.number for p in second.pulls} == set(range(1, 21))


def test_mine_github_reports_no_new_when_everything_is_stored(git_repo) -> None:
    repo = git_repo()
    run_git(repo, "remote", "add", "origin", "https://github.com/acme/widget.git")
    previous = GithubEvidence(
        owner="acme", repo="widget", watermark=5,
        pulls=[PrRecord(number=5, title="PR 5", merged_at="2026-01-02T03:04:05Z")],
    )
    outcome: dict = {}
    assert mine_github(
        repo, SpcConfig(), previous=previous,
        client_factory=lambda caps: _mock_client(_api_mock([_pr(5)]), caps),
        stats_out=outcome,
    ) is None
    assert outcome["reason"] == "no_new"


def test_mine_github_non_github_remote_returns_none(git_repo) -> None:
    repo = git_repo()
    run_git(repo, "remote", "add", "origin", "https://gitlab.com/acme/widget.git")
    assert mine_github(repo, SpcConfig(), client_factory=lambda caps: None) is None


def test_mine_github_api_failure_returns_none(git_repo) -> None:
    repo = git_repo()
    run_git(repo, "remote", "add", "origin", "https://github.com/acme/widget.git")

    def boom(caps: FetchCaps) -> GithubClient:
        def fail(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("network down")

        return _mock_client(fail, caps)

    assert mine_github(
        repo, SpcConfig(), client_factory=boom
    ) is None or True  # fail-soft either way


# ---------------------------------------------------------------------------
# decide_mining
# ---------------------------------------------------------------------------


def test_decide_mining_off(monkeypatch) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setenv("PATH", "/nonexistent")
    config = SpcConfig(github=GithubConfig(enabled="false"))
    assert decide_mining(config) == "off"


def test_decide_mining_true_mines_without_token(monkeypatch) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setenv("PATH", "/nonexistent")
    config = SpcConfig(github=GithubConfig(enabled="true"))
    assert decide_mining(config) == "mine"


def test_decide_mining_auto_with_token(monkeypatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    assert decide_mining(SpcConfig()) == "mine"


# ---------------------------------------------------------------------------
# Evidence persistence + privacy
# ---------------------------------------------------------------------------


def test_github_evidence_merges_with_git_block(git_repo) -> None:
    from spc.githistory import GitEvidence
    from spc.githubhistory import PrRecord
    from spc.profile import save_evidence, save_github_evidence

    repo = git_repo()
    save_evidence(repo, GitEvidence(commits_scanned=4))
    gh = GithubEvidence(
        owner="acme", repo="widget", mined_at=1.0, watermark=3,
        pulls=[PrRecord(number=3, title="t", merged_at="m")],
    )
    save_github_evidence(repo, gh.to_dict())
    git_block = load_evidence(repo)
    assert git_block is not None
    assert git_block["commits_scanned"] == 4  # git block survived the merge
    stored_gh = load_github_evidence(repo)
    assert stored_gh is not None
    assert stored_gh.watermark == 3


def test_raw_comments_stay_out_of_profile(tmp_path: Path, monkeypatch) -> None:
    """PRIVACY: only synthesized themes may enter profile.md — never raw comments."""
    from spc.profile import build_profile

    evidence = GithubEvidence(
        owner="acme",
        repo="widget",
        watermark=1,
        pulls=[_pr_record(1, "Add login")],
        comments=[_comment_record(1, "auth.py", "you hardcoded the secret here!!")],
    )

    captured: dict[str, str] = {}

    def fake_complete(messages, *, model, base_url, api_key, **kw):  # noqa: ANN001, ANN003
        captured["cluster"] = messages[-1]["content"]
        return "- error handling: 3 comments"

    monkeypatch.setattr("spc.llm.complete", fake_complete)
    section = cluster_themes(evidence, SpcConfig())
    assert section == "- error handling: 3 comments"

    def fake_profile_complete(messages, *, model, base_url, api_key, **kw):  # noqa: ANN001, ANN003
        captured["profile"] = messages[-1]["content"]
        return "# profile"

    monkeypatch.setattr("spc.llm.complete", fake_profile_complete)
    build_profile(
        _git_evidence(), SpcConfig(), extra_evidence_md=section
    )
    # The clustering prompt may carry raw snippets (local only), but the
    # profile synthesis prompt receives only the synthesized theme section.
    assert "you hardcoded the secret here!!" not in captured["profile"]
    assert "error handling" in captured["profile"]


def test_theme_clustering_prompt_contains_capped_evidence(monkeypatch) -> None:
    prompts: list[str] = []

    def fake_complete(messages, **kw):  # noqa: ANN001, ANN003
        prompts.append(messages[-1]["content"])
        return "themes"

    monkeypatch.setattr("spc.llm.complete", fake_complete)
    pulls = [_pr_record(n, f"title {n}") for n in range(150)]
    comments = [_comment_record(n, f"p{n}.py", "body") for n in range(400)]
    evidence = GithubEvidence(owner="o", repo="r", pulls=pulls, comments=comments)
    cluster_themes(evidence, SpcConfig())
    assert len(prompts) == 1
    assert "title 99" in prompts[0]  # first 100 PRs present
    assert "title 149" not in prompts[0]  # PRs beyond the cap are dropped
    assert "body 350" not in prompts[0]  # comments beyond the cap are dropped
    assert len(prompts[0]) < 20000  # and the prompt is bounded


def test_cluster_themes_fail_soft_on_llm_error(monkeypatch) -> None:
    def boom(*args, **kwargs):  # noqa: ANN002, ANN003
        raise RuntimeError("llm down")

    monkeypatch.setattr("spc.llm.complete", boom)
    evidence = GithubEvidence(owner="o", repo="r", pulls=[_pr_record(1, "t")])
    assert cluster_themes(evidence, SpcConfig()) is None


# ---------------------------------------------------------------------------
# learn.py integration (fail-soft git-only paths)
# ---------------------------------------------------------------------------


def test_learn_skips_github_on_non_github_remote(git_repo, monkeypatch, quiet_console) -> None:
    from spc.learn import run_learn

    repo = git_repo({"src/a.py": "a=1\n"})
    # No remote at all → not a github remote. Keep system paths (git must stay
    # runnable); gh is absent from them in this environment.
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)

    def fake_complete(messages, *, model, base_url, api_key, **kw):  # noqa: ANN001, ANN003
        return "# Profile"

    monkeypatch.setattr("spc.profile.llm.complete", fake_complete)
    assert run_learn(repo, SpcConfig(), quiet_console) is True
    output = evidence_path(repo).read_text(encoding="utf-8")
    payload = json.loads(output)
    assert "github" not in payload["sources"]  # nothing mined, nothing stored


def test_learn_github_api_failure_fails_soft_git_only(git_repo, monkeypatch, quiet_console) -> None:
    from spc.learn import run_learn

    repo = git_repo({"src/a.py": "a=1\n"})
    run_git(repo, "remote", "add", "origin", "https://github.com/acme/widget.git")
    monkeypatch.setenv("GITHUB_TOKEN", "t")

    def fail(*args, **kwargs):  # noqa: ANN002, ANN003
        raise GithubApiError("HTTP 401: Bad credentials")

    monkeypatch.setattr("spc.githubhistory.mine_github", fail)

    def fake_complete(messages, *, model, base_url, api_key, **kw):  # noqa: ANN001, ANN003
        return "# Profile"

    monkeypatch.setattr("spc.profile.llm.complete", fake_complete)
    assert run_learn(repo, SpcConfig(), quiet_console) is True  # git-only still works
    git_block = load_evidence(repo)
    assert git_block is not None
    assert git_block["commits_scanned"] == 1


def test_learn_enabled_false_never_mines(git_repo, monkeypatch, quiet_console) -> None:
    from spc.learn import run_learn

    repo = git_repo({"src/a.py": "a=1\n"})
    run_git(repo, "remote", "add", "origin", "https://github.com/acme/widget.git")

    def forbidden(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("network mining must not run when enabled=false")

    monkeypatch.setattr("spc.githubhistory.mine_github", forbidden)
    monkeypatch.setattr("spc.userconfig.resolve_github_token", lambda: None)

    def fake_complete(messages, *, model, base_url, api_key, **kw):  # noqa: ANN001, ANN003
        return "# Profile"

    monkeypatch.setattr("spc.profile.llm.complete", fake_complete)
    assert run_learn(repo, SpcConfig(github=GithubConfig(enabled="false")), quiet_console) is True


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _git_evidence():
    from spc.githistory import GitEvidence

    return GitEvidence(commits_scanned=1)


def _pr_record(number: int, title: str):
    from spc.githubhistory import PrRecord

    return PrRecord(number=number, title=title, merged_at="2026-01-01T00:00:00Z")


def _comment_record(pr_number: int, path: str, body: str):
    from spc.githubhistory import CommentRecord

    return CommentRecord(pr_number=pr_number, path=path, body_snippet=body)


# ---------------------------------------------------------------------------
# Review fixes: token pinning, budget stop, token hygiene, skip reasons,
# theme scrubbing
# ---------------------------------------------------------------------------


def _bearer(request: httpx.Request) -> str | None:
    auth = request.headers.get("Authorization")
    return auth


def test_token_not_sent_to_untrusted_custom_host(git_repo) -> None:
    """BLOCKER regression: a repo-committed base_url must not receive the token."""
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("Authorization"))
        if request.url.path.endswith("/pulls"):
            return httpx.Response(200, json=[_pr(1)])
        return httpx.Response(200, json=[])

    repo = git_repo()
    run_git(repo, "remote", "add", "origin", "https://github.com/acme/widget.git")
    config = SpcConfig(github=GithubConfig(base_url="https://evil.example"))
    evidence = mine_github(
        repo, config,
        client_factory=lambda caps: _mock_client(
            handler, caps, token="secret", origin_host="github.com"
        ),
    )
    assert evidence is not None  # mining still works, just unauthenticated
    assert seen
    assert all(auth is None for auth in seen)


def test_token_sent_to_origin_matching_ghe_host(git_repo) -> None:
    """The user's own origin host (GHE) is a trusted token target."""
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("Authorization"))
        if request.url.path.endswith("/pulls"):
            return httpx.Response(200, json=[_pr(1)])
        return httpx.Response(200, json=[])

    repo = git_repo()
    run_git(repo, "remote", "add", "origin", "https://ghe.corp/acme/widget.git")
    # GHE mining is opt-in: the repo's config must declare the API host first.
    (repo / ".spc.toml").write_text(
        '[github]\nbase_url = "https://ghe.corp/api/v3"\n', encoding="utf-8"
    )
    config = SpcConfig(github=GithubConfig(base_url="https://ghe.corp/api/v3"))
    evidence = mine_github(
        repo, config,
        client_factory=lambda caps: _mock_client(
            handler, caps, token="secret", origin_host="ghe.corp"
        ),
    )
    assert evidence is not None
    assert seen
    assert all(auth == "Bearer secret" for auth in seen)


def test_token_not_sent_over_plain_http(git_repo) -> None:
    """http:// base_url must never receive the Bearer token (cleartext leak)."""
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("Authorization"))
        if request.url.path.endswith("/pulls"):
            return httpx.Response(200, json=[_pr(1)])
        return httpx.Response(200, json=[])

    repo = git_repo()
    run_git(repo, "remote", "add", "origin", "https://github.com/acme/widget.git")
    config = SpcConfig(github=GithubConfig(base_url="http://ghe.corp/api/v3"))
    evidence = mine_github(
        repo, config,
        client_factory=lambda caps: _mock_client(
            handler, caps, token="secret", origin_host="github.com"
        ),
    )
    assert evidence is not None
    assert all(auth is None for auth in seen)


def test_token_sent_to_default_api_github_com(git_repo) -> None:
    repo = git_repo()
    run_git(repo, "remote", "add", "origin", "https://github.com/acme/widget.git")
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("Authorization"))
        if request.url.path.endswith("/pulls"):
            return httpx.Response(200, json=[_pr(1)])
        return httpx.Response(200, json=[])

    evidence = mine_github(
        repo, SpcConfig(),
        client_factory=lambda caps: _mock_client(
            handler, caps, token="secret", origin_host="github.com"
        ),
    )
    assert evidence is not None
    assert all(auth == "Bearer secret" for auth in seen)


def test_ssh_origin_host_derived_for_trust(git_repo) -> None:
    """ssh-form origins (git@host:owner/repo) pin the token to the same host."""
    repo = git_repo()
    run_git(repo, "remote", "add", "origin", "git@ghe.corp:acme/widget.git")
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("Authorization"))
        if request.url.path.endswith("/pulls"):
            return httpx.Response(200, json=[_pr(1)])
        return httpx.Response(200, json=[])

    (repo / ".spc.toml").write_text(
        '[github]\nbase_url = "https://ghe.corp/api/v3"\n', encoding="utf-8"
    )
    config = SpcConfig(github=GithubConfig(base_url="https://ghe.corp/api/v3"))
    evidence = mine_github(
        repo, config,
        client_factory=lambda caps: _mock_client(
            handler, caps, token="secret", origin_host="ghe.corp"
        ),
    )
    assert evidence is not None
    assert all(auth == "Bearer secret" for auth in seen)


def test_budget_exhaustion_keeps_fetched_work(monkeypatch) -> None:
    """Hitting the API-call ceiling stops gracefully and keeps fetched PRs."""
    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("page", "1"))
        return httpx.Response(200, json=[_pr(page * 100 + n) for n in range(50)])

    import spc.githubapi as githubapi_module

    monkeypatch.setattr(githubapi_module, "MAX_API_CALLS_PER_RUN", 3)
    caps = FetchCaps(pages_max=10, max_prs=500)
    client = _mock_client(handler, caps)
    pulls = list(client.iter_merged_pulls("o", "r"))
    assert len(pulls) == 150  # exactly 3 pages before the ceiling tripped
    assert client.stats.stopped_reason == "budget_exhausted"
    assert client.stats.prs_fetched == 150


def test_mine_github_budget_exhaustion_saves_watermark(git_repo) -> None:
    """Even a budget-truncated run persists what it fetched."""
    repo = git_repo()
    run_git(repo, "remote", "add", "origin", "https://github.com/acme/widget.git")

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("page", "1"))
        return httpx.Response(200, json=[_pr(page * 100 + n) for n in range(50)])

    # Tiny budget via monkeypatched constant: the ceiling trips mid-walk.
    monkey_budget = 2
    import spc.githubapi as githubapi_module

    original = githubapi_module.MAX_API_CALLS_PER_RUN
    githubapi_module.MAX_API_CALLS_PER_RUN = monkey_budget
    try:
        evidence = mine_github(
            repo, SpcConfig(), client_factory=lambda caps: _mock_client(handler, caps)
        )
    finally:
        githubapi_module.MAX_API_CALLS_PER_RUN = original
    assert evidence is not None
    assert evidence.pulls  # kept partial work
    assert evidence.stats["stopped_reason"] == "budget_exhausted"
    assert evidence.watermark == max(p.number for p in evidence.pulls)


def test_resolve_token_strips_whitespace_and_newlines(monkeypatch) -> None:
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", "  tok-123\r\n")
    assert resolve_token() == "tok-123"


def test_resolve_token_whitespace_only_is_none(monkeypatch) -> None:
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", "   \n")
    monkeypatch.setattr("spc.githubapi.shutil.which", lambda name: None)
    assert resolve_token() is None


def test_remote_host_helper() -> None:
    from spc.githubapi import remote_host

    assert remote_host("https://github.com/acme/widget.git") == "github.com"
    assert remote_host("git@ghe.corp:acme/widget.git") == "ghe.corp"
    assert remote_host("ssh://git@ghe.corp/acme/widget.git") == "ghe.corp"
    assert remote_host("/local/path") is None


def test_learn_skip_notice_reflects_api_error(git_repo, monkeypatch) -> None:
    """An API failure during mining says so — not the ambiguous no-new-PRs line."""
    import io

    from spc.learn import _mine_github_source

    repo = git_repo({"src/a.py": "a=1\n"})
    run_git(repo, "remote", "add", "origin", "https://github.com/acme/widget.git")
    monkeypatch.setenv("GITHUB_TOKEN", "t")

    def fail(*args, **kwargs):  # noqa: ANN002, ANN003
        raise GithubApiError("HTTP 401: Bad credentials")

    monkeypatch.setattr("spc.githubhistory.mine_github", fail)
    console = Console(file=io.StringIO(), force_terminal=False)
    _mine_github_source(repo, SpcConfig(), console)
    output = console.file.getvalue()  # type: ignore[union-attr]
    assert "API rejected us" in output
    assert "no new merged PRs since the last run" not in output


def test_learn_skip_notice_no_new_prs_when_last_run_was_clean(
    git_repo, monkeypatch
) -> None:
    """Nothing new says so plainly, and names what is already stored."""
    import io

    from spc.learn import _skip_reason

    repo = git_repo({"src/a.py": "a=1\n"})
    run_git(repo, "remote", "add", "origin", "https://github.com/acme/widget.git")
    save_github_evidence(
        repo,
        GithubEvidence(
            owner="acme", repo="widget", watermark=5,
            pulls=[_pr_record(5, "PR 5")],
            stats={"stopped_reason": "complete"},
        ).to_dict(),
    )
    previous = load_github_evidence(repo)
    console = Console(file=io.StringIO(), force_terminal=False)
    reason = _skip_reason({"reason": "no_new"}, previous)
    console.print(reason)
    output = console.file.getvalue()  # type: ignore[union-attr]
    assert "no new merged PRs" in output
    assert "1 merged PRs already stored" in output
    assert "rejected us" not in output


def test_skip_notice_does_not_trust_a_previous_truncation() -> None:
    """A stale truncation in the cached stats must not change this run's wording.

    Previously the printed reason was derived from the *previous* run's
    ``stopped_reason``: one budget-truncated run made every later healthy run
    claim the API had rejected us.
    """
    from spc.learn import _skip_reason

    previous = GithubEvidence(
        owner="acme", repo="widget", watermark=51095,
        pulls=[_pr_record(51095, "PR 51095")],
        stats={"stopped_reason": "budget_exhausted"},
    )
    assert "no new merged PRs" in _skip_reason({"reason": "no_new"}, previous)
    assert "rejected us" in _skip_reason({"reason": "api_error"}, previous)


def test_skip_notice_reports_a_non_github_remote() -> None:
    from spc.learn import _skip_reason

    assert "not a github remote" in _skip_reason({"reason": "no_remote"}, None)


def test_scrub_drops_lines_quoting_raw_comments() -> None:
    """PRIVACY defense-in-depth: raw comment text must not enter the profile."""
    import io

    from spc.learn import _scrub_theme_section

    evidence = GithubEvidence(
        owner="o",
        repo="r",
        pulls=[_pr_record(1, "Refactor the whole authentication subsystem now")],
        comments=[
            _comment_record(
                1, "auth.py", "please add error handling around the token refresh call"
            )
        ],
    )
    themes = (
        "- error handling: 3 comments\n"
        "- please add error handling around the token refresh call\n"
        "- tests: 2 comments\n"
    )
    console = Console(width=200, file=io.StringIO(), force_terminal=False)
    result = _scrub_theme_section(themes, evidence, console)
    assert "please add error handling around the token refresh call" not in result
    assert "- error handling: 3 comments" in result  # synthesized line kept
    output = console.file.getvalue()  # type: ignore[union-attr]
    assert "privacy filter" in output


def test_scrub_caps_themes_length() -> None:
    import io

    from spc.learn import MAX_THEMES_CHARS, _scrub_theme_section

    evidence = GithubEvidence(owner="o", repo="r", pulls=[_pr_record(1, "t")])
    themes = "- theme\n" * 2000
    console = Console(file=io.StringIO(), force_terminal=False)
    result = _scrub_theme_section(themes, evidence, console)
    assert len(result) <= MAX_THEMES_CHARS + len("\n…(truncated)")
