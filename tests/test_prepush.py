"""Pre-push reliability: stdin push diffs, cache enforcement, outcomes, secrets.

Feedback #1 (planning/feedback/1 - pre-push reliability.md). Uses real temp
git repos with a local bare "remote", mocked LLM.
"""

from __future__ import annotations

import json

from rich.console import Console
from typer.testing import CliRunner

from spc import cli
from spc.config import SpcConfig
from spc.diff import (
    ZERO_SHA,
    PushRef,
    parse_push_lines,
    push_diff,
)
from spc.log import history_file

runner = CliRunner()

SECRET_LINE = 'AWS_KEY = "AKIAIOSFODNN7EXAMPLE"\n'


def _null_console(monkeypatch) -> None:
    monkeypatch.setattr(cli, "console", Console(file=open("/dev/null", "w")))  # noqa: SIM115


def _patch_review(monkeypatch, payload: str = "[]") -> dict:
    """Patch the review LLM call; records the prompt it received."""
    import spc.review as review_mod

    calls: dict = {"n": 0, "prompts": []}

    def fake(messages, **kwargs):  # noqa: ANN001, ANN003
        calls["n"] += 1
        calls["prompts"].append(messages[0]["content"])
        return payload

    monkeypatch.setattr(review_mod.llm, "complete", fake)
    return calls


# --- parse_push_lines -------------------------------------------------------


def test_parse_push_lines_happy_path() -> None:
    line = f"refs/heads/main {'a' * 40} refs/heads/main {'b' * 40}\n"
    refs = parse_push_lines(line)
    assert refs == [PushRef("refs/heads/main", "a" * 40, "refs/heads/main", "b" * 40)]


def test_parse_push_lines_skips_malformed() -> None:
    refs = parse_push_lines("only three fields\n" + f"refs/heads/main {'a' * 40} refs/heads/main {'b' * 40}\n")
    assert len(refs) == 1


# --- push_diff against real repos -------------------------------------------


def _setup_repo_with_remote(git_repo, monkeypatch):
    """Repo with a bare remote, one pushed commit, one unpushed commit."""
    from conftest import run_git, run_git_capture

    repo = git_repo({"src/a.py": "a=1\n"})
    bare = repo.parent / f"{repo.name}-remote.git"
    import subprocess

    subprocess.run(["git", "init", "--bare", "-q", str(bare)], check=True, capture_output=True)
    run_git(repo, "remote", "add", "origin", str(bare))
    run_git(repo, "push", "origin", "main")

    # unpushed commit
    (repo / "src/b.py").write_text("b=2\n", encoding="utf-8")
    run_git(repo, "add", "src/b.py")
    run_git(repo, "commit", "-m", "add b")

    remote_sha = run_git_capture(repo, "rev-parse", "origin/main").strip()
    local_sha = run_git_capture(repo, "rev-parse", "main").strip()
    return repo, remote_sha, local_sha


def test_push_diff_normal_push_reviews_pushed_commits(git_repo, monkeypatch) -> None:
    repo, remote_sha, local_sha = _setup_repo_with_remote(git_repo, monkeypatch)
    refs = parse_push_lines(f"refs/heads/main {local_sha} refs/heads/main {remote_sha}\n")
    result = push_diff(repo, refs)
    assert result is not None
    assert result.source == "push"
    assert "src/b.py" in result.files
    assert "src/a.py" not in result.files  # only the pushed commit's files


def test_push_diff_branch_deletion_skipped(git_repo, monkeypatch) -> None:
    repo, _, local_sha = _setup_repo_with_remote(git_repo, monkeypatch)
    refs = parse_push_lines(
        f"refs/heads/feature {ZERO_SHA} refs/heads/feature {local_sha}\n"
        f"refs/heads/main {local_sha} refs/heads/main {local_sha}\n"
    )
    result = push_diff(repo, refs)
    assert result is None  # deletion only + identical shas (empty diff) → nothing


def test_push_diff_new_remote_ref_uses_merge_base(git_repo, monkeypatch) -> None:
    from conftest import run_git, run_git_capture

    repo, _, _ = _setup_repo_with_remote(git_repo, monkeypatch)
    run_git(repo, "checkout", "-b", "feature")
    (repo / "src/c.py").write_text("c=3\n", encoding="utf-8")
    run_git(repo, "add", "src/c.py")
    run_git(repo, "commit", "-m", "add c")
    local = run_git_capture(repo, "rev-parse", "feature").strip()

    refs = parse_push_lines(f"refs/heads/feature {local} refs/heads/feature {ZERO_SHA}\n")
    result = push_diff(repo, refs)
    assert result is not None
    assert "src/c.py" in result.files
    assert "README.md" not in result.files  # merge-base scoped it to the branch


def test_push_diff_multi_ref_concatenates(git_repo, monkeypatch) -> None:
    from conftest import run_git, run_git_capture

    repo, remote_sha, local_sha = _setup_repo_with_remote(git_repo, monkeypatch)
    base_sha = run_git_capture(repo, "rev-parse", "origin/main").strip()
    run_git(repo, "checkout", "-b", "second", base_sha)
    (repo / "src/d.py").write_text("d=4\n", encoding="utf-8")
    run_git(repo, "add", "src/d.py")
    run_git(repo, "commit", "-m", "add d")
    second = run_git_capture(repo, "rev-parse", "second").strip()

    refs = parse_push_lines(
        f"refs/heads/main {local_sha} refs/heads/main {remote_sha}\n"
        f"refs/heads/second {second} refs/heads/second {ZERO_SHA}\n"
    )
    result = push_diff(repo, refs)
    assert result is not None
    assert "src/b.py" in result.files and "src/d.py" in result.files


def test_push_diff_unresolvable_sha_skipped(git_repo, monkeypatch) -> None:
    repo, remote_sha, local_sha = _setup_repo_with_remote(git_repo, monkeypatch)
    refs = parse_push_lines(
        f"refs/heads/broken {'f' * 40} refs/heads/broken {ZERO_SHA}\n"
        f"refs/heads/main {local_sha} refs/heads/main {local_sha}\n"
    )
    # 'f'*40 is valid hex so parse keeps it — push_diff then skips the ref
    # because the SHA doesn't resolve to a commit in this repo.
    result = push_diff(repo, refs)
    assert result is None


# --- end-to-end via the review command --------------------------------------


def test_review_from_hook_reviews_pushed_commits(git_repo, monkeypatch) -> None:

    repo, remote_sha, local_sha = _setup_repo_with_remote(git_repo, monkeypatch)
    monkeypatch.chdir(repo)
    _null_console(monkeypatch)
    calls = _patch_review(monkeypatch)

    stdin = f"refs/heads/main {local_sha} refs/heads/main {remote_sha}\n"
    result = runner.invoke(cli.app, ["review", "--from-hook"], input=stdin)
    assert result.exit_code == 0, result.output
    assert calls["n"] == 1
    assert "src/b.py" in calls["prompts"][0]
    assert "src/a.py" not in calls["prompts"][0]



def test_review_from_hook_empty_stdin_exits_silently(git_repo, monkeypatch) -> None:
    repo, _, _ = _setup_repo_with_remote(git_repo, monkeypatch)
    monkeypatch.chdir(repo)
    _null_console(monkeypatch)
    calls = _patch_review(monkeypatch)

    result = runner.invoke(cli.app, ["review", "--from-hook"], input="")
    assert result.exit_code == 0
    assert calls["n"] == 0  # nothing to review — no LLM call


def test_review_from_hook_force_push_reviews_new_commits(git_repo, monkeypatch) -> None:
    """Force push: local history rewritten; two-dot diff still reviews the pushed tip."""
    from conftest import run_git, run_git_capture

    repo, remote_sha, _ = _setup_repo_with_remote(git_repo, monkeypatch)
    run_git(repo, "reset", "--hard", "HEAD~1")  # undo unpushed commit
    (repo / "src/rewrite.py").write_text("r=1\n", encoding="utf-8")
    run_git(repo, "add", "src/rewrite.py")
    run_git(repo, "commit", "-m", "rewritten commit")
    run_git(repo, "commit", "--amend", "-m", "rewritten commit (amended)")
    local_sha = run_git_capture(repo, "rev-parse", "main").strip()

    monkeypatch.chdir(repo)
    _null_console(monkeypatch)
    calls = _patch_review(monkeypatch)

    stdin = f"refs/heads/main {local_sha} refs/heads/main {remote_sha}\n"
    result = runner.invoke(cli.app, ["review", "--from-hook"], input=stdin)
    assert result.exit_code == 0, result.output
    assert "src/rewrite.py" in calls["prompts"][0]


def test_review_manual_mode_unchanged(git_repo, monkeypatch) -> None:
    """Without --from-hook, the upstream→staged fallback behavior is intact."""
    from conftest import run_git

    repo, _, _ = _setup_repo_with_remote(git_repo, monkeypatch)
    (repo / "src/staged.py").write_text("s=1\n", encoding="utf-8")
    run_git(repo, "add", "src/staged.py")
    monkeypatch.chdir(repo)
    _null_console(monkeypatch)
    calls = _patch_review(monkeypatch)

    result = runner.invoke(cli.app, ["review"], input="")
    assert result.exit_code == 0, result.output
    assert "src/staged.py" in calls["prompts"][0]  # staged fallback reviewed


def test_hook_block_uses_from_hook_flag() -> None:
    from spc.hook import HOOK_BLOCK

    assert "spc review --from-hook" in HOOK_BLOCK


# --- cache enforcement -------------------------------------------------------


def _record_payload(severity: str = "critical") -> str:
    return json.dumps(
        [{"severity": severity, "file": "src/b.py", "line": 1, "message": "m", "suggestion": ""}]
    )


def test_cached_critical_blocks_when_strict(git_repo, monkeypatch) -> None:

    repo, remote_sha, local_sha = _setup_repo_with_remote(git_repo, monkeypatch)
    monkeypatch.chdir(repo)
    reading = Console(record=True, width=100)
    monkeypatch.setattr(cli, "console", reading)

    payload = _record_payload("critical")
    calls = _patch_review(monkeypatch, payload)

    config = SpcConfig(review=SpcConfig().review.__class__(strict=True))
    stdin = f"refs/heads/main {local_sha} refs/heads/main {remote_sha}\n"
    # first run: fresh review, strict+critical → 1
    assert cli.execute_review(repo, config, push_stdin=stdin) == 1
    assert calls["n"] == 1
    # second run: cache hit — the cached critical must STILL block
    assert cli.execute_review(repo, config, push_stdin=stdin) == 1
    assert calls["n"] == 1  # no fresh LLM call — served from cache
    assert "push blocked" in reading.export_text()


def test_cached_critical_no_strict_exits_zero(git_repo, monkeypatch) -> None:
    repo, remote_sha, local_sha = _setup_repo_with_remote(git_repo, monkeypatch)
    monkeypatch.chdir(repo)
    _null_console(monkeypatch)
    _patch_review(monkeypatch, _record_payload("critical"))

    stdin = f"refs/heads/main {local_sha} refs/heads/main {remote_sha}\n"
    assert cli.execute_review(repo, SpcConfig(), push_stdin=stdin) == 0
    # cached, non-strict → findings printed, exit 0
    assert cli.execute_review(repo, SpcConfig(), push_stdin=stdin) == 0


def test_failed_review_not_logged_and_not_cached(git_repo, monkeypatch) -> None:
    """LLM failure: no JSONL record → next push re-reviews (no fake cache pass)."""
    import spc.review as review_mod

    repo, remote_sha, local_sha = _setup_repo_with_remote(git_repo, monkeypatch)
    monkeypatch.chdir(repo)
    reading = Console(record=True, width=100)
    monkeypatch.setattr(cli, "console", reading)

    def fail(*args, **kwargs):  # noqa: ANN002, ANN003
        raise review_mod.llm.LlmError("LLM down")

    monkeypatch.setattr(review_mod.llm, "complete", fail)
    stdin = f"refs/heads/main {local_sha} refs/heads/main {remote_sha}\n"

    assert cli.execute_review(repo, SpcConfig(), push_stdin=stdin) == 0
    assert "no substantive findings" not in reading.export_text()  # never green-pass a failure
    records = [json.loads(line) for line in history_file(repo).read_text(encoding="utf-8").splitlines() if line.strip()] if history_file(repo).exists() else []
    assert records == []  # failed review logged nothing

    # second identical push: NOT a cache hit — the LLM is called again
    calls = {"n": 0}

    def counting_fail(*args, **kwargs):  # noqa: ANN002, ANN003
        calls["n"] += 1
        raise review_mod.llm.LlmError("LLM down")

    monkeypatch.setattr(review_mod.llm, "complete", counting_fail)
    cli.execute_review(repo, SpcConfig(), push_stdin=stdin)
    assert calls["n"] == 1  # re-reviewed, not served from cache


def test_outcome_field_written_to_jsonl(git_repo, monkeypatch) -> None:
    repo, remote_sha, local_sha = _setup_repo_with_remote(git_repo, monkeypatch)
    monkeypatch.chdir(repo)
    _null_console(monkeypatch)
    _patch_review(monkeypatch, "[]")

    stdin = f"refs/heads/main {local_sha} refs/heads/main {remote_sha}\n"
    assert cli.execute_review(repo, SpcConfig(), push_stdin=stdin) == 0
    record = json.loads(history_file(repo).read_text(encoding="utf-8").strip().splitlines()[-1])
    assert record["outcome"] == "passed"


def test_findings_outcome_written_to_jsonl(git_repo, monkeypatch) -> None:
    repo, remote_sha, local_sha = _setup_repo_with_remote(git_repo, monkeypatch)
    monkeypatch.chdir(repo)
    _null_console(monkeypatch)
    _patch_review(monkeypatch, _record_payload("warn"))

    stdin = f"refs/heads/main {local_sha} refs/heads/main {remote_sha}\n"
    cli.execute_review(repo, SpcConfig(), push_stdin=stdin)
    record = json.loads(history_file(repo).read_text(encoding="utf-8").strip().splitlines()[-1])
    assert record["outcome"] == "findings"


def test_cached_incomplete_record_is_not_served(git_repo, monkeypatch) -> None:
    """A cached 'incomplete' record must not become a silent green pass.

    Regression: a truncated diff that came back clean was cached with
    outcome=incomplete; the next identical push served green
    "no substantive findings" without any truncation warning — the reader
    could not tell the review never saw the whole diff.
    """
    from conftest import run_git, run_git_capture

    repo, remote_sha, _ = _setup_repo_with_remote(git_repo, monkeypatch)
    big = "x = 1\n" * 100_000  # ~600 KB, over the default 200 KB cap
    (repo / "src/big0.py").write_text(big, encoding="utf-8")
    run_git(repo, "add", "src/big0.py")
    run_git(repo, "commit", "-m", "big files")
    local_sha = run_git_capture(repo, "rev-parse", "main").strip()
    monkeypatch.chdir(repo)
    reading = Console(record=True, width=100)
    monkeypatch.setattr(cli, "console", reading)
    calls = _patch_review(monkeypatch, "[]")  # LLM returns clean

    stdin = f"refs/heads/main {local_sha} refs/heads/main {remote_sha}\n"
    # First run: diff truncated → warning printed, outcome=incomplete, logged.
    assert cli.execute_review(repo, SpcConfig(), push_stdin=stdin) == 0
    assert "review incomplete" in reading.export_text()
    record = json.loads(history_file(repo).read_text(encoding="utf-8").strip().splitlines()[-1])
    assert record["outcome"] == "incomplete"

    # Second identical push: the incomplete record is NOT a cache hit —
    # the LLM is called again and nothing green is printed.
    assert cli.execute_review(repo, SpcConfig(), push_stdin=stdin) == 0
    assert calls["n"] == 2
    assert "cached findings" not in reading.export_text()


# --- secrets -----------------------------------------------------------------


def test_secrets_redacted_before_llm_by_default(git_repo, monkeypatch) -> None:
    from conftest import run_git

    repo, _, _ = _setup_repo_with_remote(git_repo, monkeypatch)
    (repo / "config.py").write_text(SECRET_LINE, encoding="utf-8")
    run_git(repo, "add", "config.py")
    run_git(repo, "commit", "-m", "add secret")

    import subprocess

    local_sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "main"], capture_output=True, text=True, check=True
    ).stdout.strip()
    remote_sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "origin/main"], capture_output=True, text=True, check=True
    ).stdout.strip()

    monkeypatch.chdir(repo)
    _null_console(monkeypatch)
    calls = _patch_review(monkeypatch)

    stdin = f"refs/heads/main {local_sha} refs/heads/main {remote_sha}\n"
    result = runner.invoke(cli.app, ["review", "--from-hook"], input=stdin)
    assert result.exit_code == 0, result.output
    prompt = calls["prompts"][0]
    assert "AKIAIOSFODNN7EXAMPLE" not in prompt  # secret never sent
    assert "[REDACTED:" in prompt  # marker present instead


def test_block_on_secrets_blocks_push(git_repo, monkeypatch) -> None:
    from conftest import run_git

    repo, remote_sha, _ = _setup_repo_with_remote(git_repo, monkeypatch)
    (repo / "config.py").write_text(SECRET_LINE, encoding="utf-8")
    run_git(repo, "add", "config.py")
    run_git(repo, "commit", "-m", "add secret")

    import subprocess

    local_sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "main"], capture_output=True, text=True, check=True
    ).stdout.strip()

    monkeypatch.chdir(repo)
    reading = Console(record=True, width=100)
    monkeypatch.setattr(cli, "console", reading)
    calls = _patch_review(monkeypatch)

    config = SpcConfig(secrets=SpcConfig().secrets.__class__(block_on_secrets=True))
    stdin = f"refs/heads/main {local_sha} refs/heads/main {remote_sha}\n"
    assert cli.execute_review(repo, config, push_stdin=stdin) == 1  # BLOCKS
    assert calls["n"] == 0  # LLM not called


# --- cache validity ----------------------------------------------------------


def test_conventions_change_invalidates_cache(git_repo, monkeypatch) -> None:
    from spc.log import cache_key

    base = dict(
        diff_hash="x", model="m", strict=False, show_nits=False,
        profile_hash="p", conventions_hash="c1",
    )
    key1 = cache_key(**base)
    key2 = cache_key(**{**base, "conventions_hash": "c2"})
    assert key1 != key2  # convention change → re-review


# --- truncate → incomplete ---------------------------------------------------


def test_truncation_marks_review_incomplete(git_repo, monkeypatch) -> None:
    from conftest import run_git, run_git_capture

    repo, remote_sha, _ = _setup_repo_with_remote(git_repo, monkeypatch)
    big = "x = 1\n" * 100_000  # ~600 KB, over the default 200 KB cap
    (repo / "src/big0.py").write_text(big, encoding="utf-8")
    run_git(repo, "add", "src/big0.py")
    run_git(repo, "commit", "-m", "big files")
    local_sha = run_git_capture(repo, "rev-parse", "main").strip()  # HEAD AFTER big commit

    monkeypatch.chdir(repo)
    reading = Console(record=True, width=100)
    monkeypatch.setattr(cli, "console", reading)
    _patch_review(monkeypatch)

    stdin = f"refs/heads/main {local_sha} refs/heads/main {remote_sha}\n"
    result = cli.execute_review(repo, SpcConfig(), push_stdin=stdin)
    assert result == 0
    assert "review incomplete" in reading.export_text()
    record = json.loads(history_file(repo).read_text(encoding="utf-8").strip().splitlines()[-1])
    assert record["outcome"] == "incomplete"
