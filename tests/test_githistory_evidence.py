"""Structured git evidence: commit records, relationships, schema, excerpts.

Feedback #2 (planning/feedback/2 - git history-mining.md). Synthetic repos via
the ``git_repo`` fixture (real git, no network), plus a few direct-git cases
for renames, merges, and reverted commits.
"""

from __future__ import annotations

import subprocess
from io import StringIO
from pathlib import Path

from conftest import (
    AWS_FIXTURE_KEY,
    private_key_block,
    run_git,
    run_git_capture,
)
from rich.console import Console

from spc.githistory import (
    GIT_EVIDENCE_SCHEMA,
    MAX_EXCERPT_COMMIT_BYTES,
    MAX_EXCERPT_FILES,
    RELATIONSHIP_CO_CHANGE,
    RELATIONSHIP_FIX_AFTER_CHANGE,
    RELATIONSHIP_FIX_WITHOUT_TEST,
    RELATIONSHIP_REVERT,
    GitEvidence,
    _is_test_path,
    evidence_from_block,
    evidence_to_block,
    mine_git_history,
)


def _of_kind(evidence, kind: str):
    return [rel for rel in evidence.relationships if rel.kind == kind]


# --- commit records ---------------------------------------------------------


def test_commit_records_carry_sha_dates_subject_and_file_changes(git_repo) -> None:
    repo = git_repo(
        {"src/a.py": "a = 1\n"},
        later_commits=[("fix: correct a", {"src/a.py": "a = 2\nb = 3\n"})],
    )
    evidence = mine_git_history(repo, max_commits=50)
    assert evidence is not None
    assert evidence.commits_scanned == 2

    fix_commit = next(
        commit for commit in _all_commits(repo, evidence) if commit.subject == "fix: correct a"
    )
    assert len(fix_commit.sha) == 40
    assert fix_commit.author_timestamp > 0
    assert fix_commit.committer_timestamp > 0
    assert [change.path for change in fix_commit.files] == ["src/a.py"]
    assert fix_commit.files[0].added == 2
    assert fix_commit.files[0].deleted == 1
    assert fix_commit.files[0].change_type == "modified"


def _all_commits(repo: Path, evidence) -> list:
    """Persisted records plus every scanned commit, for assertions on non-cited ones.

    Persistence keeps only relationship-involved commits by design, so tests that
    care about arbitrary commits re-read the log directly through the parser.
    The format comes from ``LOG_FORMAT`` — a hand-copied string silently drifts
    from production (an earlier copy omitted ``%P``, hiding merge handling).
    """
    from spc.diff import run_git as diff_run_git
    from spc.githistory import LOG_FORMAT, _parse_log

    log = diff_run_git(repo, "log", "--numstat", "-M", LOG_FORMAT, "--")
    return _parse_log(log or "")


def test_rename_is_recorded_with_old_path(git_repo) -> None:
    repo = git_repo({"src/old.py": "x = 1\n"})
    run_git(repo, "mv", "src/old.py", "src/new.py")
    run_git(repo, "commit", "-m", "refactor: rename module")

    evidence = mine_git_history(repo, max_commits=50)
    assert evidence is not None
    rename_commit = _all_commits(repo, evidence)[0]
    change = next(change for change in rename_commit.files if change.path == "src/new.py")
    assert change.change_type == "renamed"
    assert change.old_path == "src/old.py"


def test_merge_commit_does_not_crash_mining(git_repo) -> None:
    repo = git_repo({"src/a.py": "a = 1\n"})
    run_git(repo, "checkout", "-b", "feature")
    (repo / "src" / "b.py").write_text("b = 1\n", encoding="utf-8")
    run_git(repo, "add", "src/b.py")
    run_git(repo, "commit", "-m", "feat: add b")
    run_git(repo, "checkout", "main")
    (repo / "src" / "c.py").write_text("c = 1\n", encoding="utf-8")
    run_git(repo, "add", "src/c.py")
    run_git(repo, "commit", "-m", "feat: add c")
    run_git(repo, "merge", "--no-ff", "-m", "merge feature", "feature")

    evidence = mine_git_history(repo, max_commits=50)
    assert evidence is not None
    assert evidence.commits_scanned >= 4
    subjects = {commit.subject for commit in _all_commits(repo, evidence)}
    assert "merge feature" in subjects


# --- relationships: observed vs hypothesis ----------------------------------


def test_revert_links_to_reverted_commit_as_observed(git_repo) -> None:
    repo = git_repo({"src/a.py": "a = 1\n"})
    (repo / "src" / "a.py").write_text("a = 2\n", encoding="utf-8")
    run_git(repo, "add", "src/a.py")
    run_git(repo, "commit", "-m", "feat: risky change")
    bad_sha = run_git_capture(repo, "rev-parse", "HEAD").strip()
    run_git(repo, "revert", "--no-edit", "HEAD")

    evidence = mine_git_history(repo, max_commits=50)
    assert evidence is not None
    reverts = _of_kind(evidence, RELATIONSHIP_REVERT)
    assert len(reverts) == 1
    assert reverts[0].observed is True
    assert bad_sha in reverts[0].commits
    assert reverts[0].files == ("src/a.py",)


def test_fix_after_change_is_a_hypothesis_not_a_claim(git_repo) -> None:
    repo = git_repo(
        {"src/auth.py": "def login(): pass\n"},
        later_commits=[
            ("feat: add login guard", {"src/auth.py": "def login(): return 1\n"}),
            ("fix: login crashes on empty token", {"src/auth.py": "def login(): return 2\n"}),
        ],
    )
    evidence = mine_git_history(repo, max_commits=50)
    assert evidence is not None
    followups = _of_kind(evidence, RELATIONSHIP_FIX_AFTER_CHANGE)
    assert len(followups) == 1
    relationship = followups[0]
    assert relationship.observed is False
    assert relationship.files == ("src/auth.py",)
    assert len(relationship.commits) == 2


def test_unrelated_fix_is_not_linked(git_repo) -> None:
    repo = git_repo(
        {"src/auth.py": "a = 1\n", "docs/readme.py": "d = 1\n"},
        later_commits=[
            ("feat: auth work", {"src/auth.py": "a = 2\n"}),
            ("fix: typo in docs", {"docs/readme.py": "d = 2\n"}),
        ],
    )
    evidence = mine_git_history(repo, max_commits=50)
    assert evidence is not None
    assert _of_kind(evidence, RELATIONSHIP_FIX_AFTER_CHANGE) == []


def test_fix_with_tests_is_not_reported_as_untested(git_repo) -> None:
    repo = git_repo(
        {"src/a.py": "a = 1\n", "tests/test_a.py": "def test_a(): ...\n"},
        later_commits=[
            ("fix: correct a", {
                "src/a.py": "a = 2\n",
                "tests/test_a.py": "def test_a(): assert True\n",
            }),
        ],
    )
    evidence = mine_git_history(repo, max_commits=50)
    assert evidence is not None
    assert _of_kind(evidence, RELATIONSHIP_FIX_WITHOUT_TEST) == []


def test_fix_without_tests_is_observed(git_repo) -> None:
    repo = git_repo(
        {"src/a.py": "a = 1\n", "tests/test_a.py": "def test_a(): ...\n"},
        later_commits=[("fix: correct a", {"src/a.py": "a = 2\n"})],
    )
    evidence = mine_git_history(repo, max_commits=50)
    assert evidence is not None
    untested = _of_kind(evidence, RELATIONSHIP_FIX_WITHOUT_TEST)
    assert len(untested) == 1
    assert untested[0].observed is True
    assert untested[0].files == ("src/a.py",)


def test_revert_is_not_counted_as_an_untested_fix(git_repo) -> None:
    repo = git_repo({"src/a.py": "a = 1\n"})
    (repo / "src" / "a.py").write_text("a = 2\n", encoding="utf-8")
    run_git(repo, "add", "src/a.py")
    run_git(repo, "commit", "-m", "feat: change a")
    run_git(repo, "revert", "--no-edit", "HEAD")

    evidence = mine_git_history(repo, max_commits=50)
    assert evidence is not None
    assert _of_kind(evidence, RELATIONSHIP_REVERT)
    assert _of_kind(evidence, RELATIONSHIP_FIX_WITHOUT_TEST) == []
    assert _of_kind(evidence, RELATIONSHIP_FIX_AFTER_CHANGE) == []


def test_co_change_pairs_require_repeated_support(git_repo) -> None:
    repo = git_repo(
        {"src/a.py": "a = 0\n", "src/b.py": "b = 0\n", "src/lonely.py": "l = 0\n"},
        later_commits=[
            ("feat: touch a and b", {"src/a.py": "a = 1\n", "src/b.py": "b = 1\n"}),
            ("feat: touch a and b again", {"src/a.py": "a = 2\n", "src/b.py": "b = 2\n"}),
            ("feat: touch a and b once more", {"src/a.py": "a = 3\n", "src/b.py": "b = 3\n"}),
        ],
    )
    evidence = mine_git_history(repo, max_commits=50)
    assert evidence is not None
    pairs = _of_kind(evidence, RELATIONSHIP_CO_CHANGE)
    assert len(pairs) == 1
    assert pairs[0].files == ("src/a.py", "src/b.py")
    assert pairs[0].observed is True
    # The initial commit creates both files too, so support is 4, not 3.
    assert "4 commits" in pairs[0].note


def test_single_co_change_is_below_support_threshold(git_repo) -> None:
    repo = git_repo(
        {"src/a.py": "a = 0\n", "src/b.py": "b = 0\n"},
        later_commits=[("feat: once only", {"src/a.py": "a = 1\n", "src/b.py": "b = 1\n"})],
    )
    evidence = mine_git_history(repo, max_commits=50)
    assert evidence is not None
    assert _of_kind(evidence, RELATIONSHIP_CO_CHANGE) == []


def test_references_are_extracted_from_message(git_repo) -> None:
    repo = git_repo({"src/a.py": "a = 1\n"})
    (repo / "src" / "a.py").write_text("a = 2\n", encoding="utf-8")
    run_git(repo, "add", "src/a.py")
    run_git(repo, "commit", "-m", "fix: guard login", "-m", "Closes #412, see also #77.")

    evidence = mine_git_history(repo, max_commits=50)
    assert evidence is not None
    commit = _all_commits(repo, evidence)[0]
    assert commit.references == ("412", "77")


# --- excerpts ---------------------------------------------------------------


def test_excerpts_are_bounded_and_only_for_relationship_commits(git_repo) -> None:
    repo = git_repo({"src/a.py": "a = 1\n"})
    (repo / "src" / "a.py").write_text("a = 2\n", encoding="utf-8")
    run_git(repo, "add", "src/a.py")
    run_git(repo, "commit", "-m", "feat: change a")
    run_git(repo, "revert", "--no-edit", "HEAD")

    evidence = mine_git_history(repo, max_commits=50)
    assert evidence is not None
    assert evidence.excerpts_enabled is True
    assert evidence.commits  # only cited commits persisted
    assert len(evidence.commits) < evidence.commits_scanned
    for commit in evidence.commits:
        assert commit.excerpt
        assert len(commit.excerpt) < 2000


def test_excerpts_disabled_fetches_no_code(git_repo) -> None:
    repo = git_repo({"src/a.py": "a = 1\n"})
    (repo / "src" / "a.py").write_text("a = 2\n", encoding="utf-8")
    run_git(repo, "add", "src/a.py")
    run_git(repo, "commit", "-m", "feat: change a")
    run_git(repo, "revert", "--no-edit", "HEAD")

    evidence = mine_git_history(repo, max_commits=50, collect_excerpts=False)
    assert evidence is not None
    assert evidence.excerpts_enabled is False
    assert all(not commit.excerpt for commit in evidence.commits)


def test_excerpt_budget_follows_cascades_not_citation_order(git_repo, monkeypatch) -> None:
    """Citation order is detector order: every revert is cited before any fix.

    On a 2000-commit window that meant all 60 excerpt slots went to the revert
    cohort and none to the fixes a single change provoked — the one signal the
    profile cannot derive any other way. With a budget of two, the cascade must
    win and the reverts must lose.
    """
    from spc import githistory

    repo = git_repo({"src/a.py": "a = 1\n", "src/b.py": "b = 1\n"})
    (repo / "src" / "a.py").write_text("a = 2\n", encoding="utf-8")
    (repo / "src" / "b.py").write_text("b = 2\n", encoding="utf-8")
    run_git(repo, "add", "src/a.py", "src/b.py")
    run_git(repo, "commit", "-m", "feat: rename the cache parameter")
    # The two follow-ups touch different files, so neither can link to the
    # other — both point back at the rename, which is what makes a cascade.
    (repo / "src" / "a.py").write_text("a = 3\n", encoding="utf-8")
    run_git(repo, "add", "src/a.py")
    run_git(repo, "commit", "-m", "fix: follow-up in a")
    (repo / "src" / "b.py").write_text("b = 3\n", encoding="utf-8")
    run_git(repo, "add", "src/b.py")
    run_git(repo, "commit", "-m", "fix: follow-up in b")
    (repo / "src" / "c.py").write_text("c = 1\n", encoding="utf-8")
    run_git(repo, "add", "src/c.py")
    run_git(repo, "commit", "-m", "feat: risky c")
    run_git(repo, "revert", "--no-edit", "HEAD")

    monkeypatch.setattr(githistory, "MAX_EXCERPT_COMMITS", 2)
    evidence = githistory.mine_git_history(repo, max_commits=50)
    assert evidence is not None

    by_sha = {commit.sha: commit for commit in evidence.commits}
    parent = next(c for c in evidence.commits if "rename the cache" in c.subject)
    revert = next(c for c in evidence.commits if c.subject.startswith("Revert"))

    assert parent.excerpt, "the cascade parent lost its excerpt to the revert cohort"
    assert not by_sha[revert.sha].excerpt, "the revert took a budget it cannot spend"


def test_excerpt_redacts_secret_removed_by_a_fix(git_repo) -> None:
    """A historical fix often shows a secret being removed — the ``-`` side must be clean too."""
    secret = AWS_FIXTURE_KEY
    repo = git_repo({"src/config.py": f'key = "{secret}"\n'})
    (repo / "src" / "config.py").write_text('key = "safe"\n', encoding="utf-8")
    run_git(repo, "add", "src/config.py")
    run_git(repo, "commit", "-m", "fix: stop hardcoding the key")

    evidence = mine_git_history(repo, max_commits=50)
    assert evidence is not None
    excerpts = " ".join(commit.excerpt for commit in evidence.commits)
    assert secret not in excerpts
    assert "[REDACTED" in excerpts


def test_excerpt_redacts_private_key_block_body(git_repo) -> None:
    body = "\n".join(f"MIIEpAIBAAKCAQEA{i}" for i in range(4))
    repo = git_repo(
        {"src/key.pem": private_key_block([body])}
    )
    (repo / "src/key.pem").write_text("placeholder\n", encoding="utf-8")
    run_git(repo, "add", "src/key.pem")
    run_git(repo, "commit", "-m", "fix: remove committed private key")

    evidence = mine_git_history(repo, max_commits=50)
    assert evidence is not None
    excerpts = " ".join(commit.excerpt for commit in evidence.commits)
    assert "MIIEpAIBAAKCAQEA" not in excerpts
    assert "[REDACTED:private key body]" in excerpts


def test_excerpt_redacts_rotated_private_key_whose_header_is_context(git_repo) -> None:
    """A key rotation leaves ``-----BEGIN`` unchanged, so it arrives as CONTEXT.

    Trimming drops context lines. If redaction ran after trimming, the header
    would be gone, body redaction would never fire, and the new key material
    would land in evidence.json and the synthesis prompt.
    """
    repo = git_repo({"deploy/key.pem": private_key_block(["OLDKEYBODYLINE1"])})
    (repo / "deploy" / "key.pem").write_text(
        private_key_block(["NEWKEYBODYLINE1"]),
        encoding="utf-8",
    )
    run_git(repo, "add", "deploy/key.pem")
    run_git(repo, "commit", "-m", "fix: rotate the deploy key")

    evidence = mine_git_history(repo, max_commits=50)
    assert evidence is not None
    excerpts = " ".join(commit.excerpt for commit in evidence.commits)
    assert "NEWKEYBODYLINE1" not in excerpts
    assert "OLDKEYBODYLINE1" not in excerpts
    assert "[REDACTED:private key body]" in excerpts


def test_excerpt_redacts_key_material_when_the_header_is_out_of_view(git_repo) -> None:
    """A rotation deep inside a key file shows NO ``-----BEGIN`` line at all.

    ``--unified=1`` context is one line, so a header 20 lines up never appears;
    body redaction has nothing to anchor on, and base64 matches no single-line
    secret pattern. The body must still be recognised as key material.
    """
    body = [f"BODYLINENUMBER{index:02d}AbCdEfGhIjKlMnOpQrStUvWxYz0123456789+/" for index in range(30)]
    repo_key = git_repo({"deploy/key.pem": private_key_block(body)})
    rotated = list(body)
    rotated[20] = "U0VDUkVUTUFURVJJQUxMSU5FMjBaWlpaWlpaWlpaWlpaWlpaWlpa"
    (repo_key / "deploy" / "key.pem").write_text(
        private_key_block(rotated), encoding="utf-8"
    )
    run_git(repo_key, "add", "deploy/key.pem")
    run_git(repo_key, "commit", "-m", "fix: rotate the deploy key")

    evidence = mine_git_history(repo_key, max_commits=50)
    assert evidence is not None
    excerpts = " ".join(commit.excerpt for commit in evidence.commits)
    assert "U0VDUkVU" not in excerpts, "the new key material leaked"
    assert "BODYLINENUMBER17" not in excerpts, "the neighbouring key material leaked via hunk context"
    assert "[REDACTED:private key body]" in excerpts


def test_excerpt_survives_non_utf8_content(git_repo) -> None:
    """One latin-1 byte in a cited commit must not abort `spc learn` or `spc init`."""
    repo = git_repo({"src/a.py": "a = 1\n"})
    (repo / "src" / "a.py").write_bytes(b"a = 2  # caf\xe9\n")
    run_git(repo, "add", "src/a.py")
    run_git(repo, "commit", "-m", "fix: correct a")

    evidence = mine_git_history(repo, max_commits=50)
    assert evidence is not None
    assert evidence.commits_scanned == 2


def test_excerpt_is_byte_capped_for_one_giant_line(git_repo) -> None:
    """A single minified line must not consume the whole excerpt budget.

    The cap also must not throw away the evidence: for a minified bundle that
    one line IS the change, so trimming back to the last newline would leave
    only headers and a ``-`` line.
    """
    repo = git_repo({"src/bundle.js": "var a=1;\n"})
    (repo / "src" / "bundle.js").write_text("var a=1;" + "x" * 3_000_000 + "\n", encoding="utf-8")
    run_git(repo, "add", "src/bundle.js")
    run_git(repo, "commit", "-m", "fix: bundle bug")

    evidence = mine_git_history(repo, max_commits=50)
    assert evidence is not None
    excerpts = [commit.excerpt for commit in evidence.commits if commit.excerpt]
    assert excerpts
    for excerpt in excerpts:
        assert len(excerpt.encode("utf-8")) <= MAX_EXCERPT_COMMIT_BYTES + 64
        assert "xxxxxxxx" in excerpt
        assert excerpt.rstrip().endswith("…(truncated)")


def test_excerpt_caps_file_headers_for_a_multi_file_commit(git_repo) -> None:
    """A rename of hundreds of files must not emit hundreds of ``diff --git`` lines."""
    files = {f"src/f{index}.py": "x = 1\n" for index in range(20)}
    repo = git_repo(files)
    for index in range(20):
        (repo / "src" / f"f{index}.py").write_text("x = 2\n", encoding="utf-8")
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-m", "fix: touch everything")

    evidence = mine_git_history(repo, max_commits=50)
    assert evidence is not None
    combined = " ".join(commit.excerpt for commit in evidence.commits)
    assert combined.count("diff --git ") <= MAX_EXCERPT_FILES + 1


# --- parsing robustness -----------------------------------------------------


def test_commit_body_cannot_corrupt_a_merge_commit_record(git_repo) -> None:
    """Separator characters in a body damage only the body; ``%P`` comes first."""
    repo = git_repo({"src/a.py": "a = 1\n"})
    run_git(repo, "checkout", "-b", "feature")
    (repo / "src" / "b.py").write_text("b = 1\n", encoding="utf-8")
    run_git(repo, "add", "src/b.py")
    run_git(repo, "commit", "-m", "feat: add b")
    run_git(repo, "checkout", "main")
    (repo / "src" / "c.py").write_text("c = 1\n", encoding="utf-8")
    run_git(repo, "add", "src/c.py")
    run_git(repo, "commit", "-m", "feat: add c")
    # A merge message carrying both git separators must not be read as numstat.
    body = "merge body\x1fwith separators\x1eand more"
    run_git(repo, "merge", "--no-ff", "-m", "merge feature", "-m", body, "feature")

    evidence = mine_git_history(repo, max_commits=50)
    assert evidence is not None
    commits = _all_commits(repo, evidence)
    merges = [commit for commit in commits if len(commit.parents) > 1]
    assert len(merges) == 1
    assert merges[0].subject == "merge feature"
    # Field order, not just "a merge exists": a body read as ``%P`` still parses
    # into *some* record, so pin the exact parents and every non-merge's arity.
    expected_parents = tuple(
        run_git_capture(repo, "rev-list", "--parents", "-n", "1", "HEAD").split()[1:]
    )
    assert merges[0].parents == expected_parents
    assert merges[0].sha == run_git_capture(repo, "rev-parse", "HEAD").strip()
    non_merges = [commit for commit in commits if len(commit.parents) <= 1]
    assert non_merges
    # Every non-merge has exactly one parent, except the root commit (none).
    assert all(len(commit.parents) == 1 for commit in non_merges if commit.parents)
    assert sum(1 for commit in non_merges if not commit.parents) == 1


# --- relationship honesty ---------------------------------------------------


def test_merge_commits_contribute_no_file_evidence(git_repo) -> None:
    """Merge records carry no changed files, so they cannot inflate co-change support.

    ``git log --numstat`` (without ``-m``) emits no numstat for a merge, and its
    parents are recorded separately. Both properties are what keep a merge from
    being counted as ordinary work: a merge would otherwise add support for
    every pair it lands, and appear as an "observed" co-change.
    """
    repo = git_repo({"src/a.py": "a = 1\n", "src/b.py": "b = 1\n"})
    for index in range(3):
        for name in ("a", "b"):
            (repo / "src" / f"{name}.py").write_text(f"v = {index}\n", encoding="utf-8")
        run_git(repo, "add", "-A")
        run_git(repo, "commit", "-m", f"feat: tune {index}")
    run_git(repo, "checkout", "-b", "feature")
    (repo / "src" / "a.py").write_text("v = 99\n", encoding="utf-8")
    (repo / "src" / "b.py").write_text("v = 99\n", encoding="utf-8")
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-m", "feat: branch touches both")
    run_git(repo, "checkout", "main")
    (repo / "src" / "c.py").write_text("c = 1\n", encoding="utf-8")
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-m", "feat: add c")
    run_git(repo, "merge", "--no-ff", "-m", "fix: merge feature", "feature")

    evidence = mine_git_history(repo, max_commits=50)
    assert evidence is not None
    merges = [commit for commit in _all_commits(repo, evidence) if len(commit.parents) > 1]
    assert len(merges) == 1, "fixture must contain exactly one merge"
    assert merges[0].files == ()
    merge_shas = {commit.sha for commit in merges}

    co_changes = _of_kind(evidence, RELATIONSHIP_CO_CHANGE)
    assert co_changes, "fixture must produce a co-change relationship"
    for relationship in co_changes:
        assert not (merge_shas & set(relationship.commits)), relationship.note
    # Initial commit + 3 tuning commits + the branch commit touched both files;
    # the merge would have made it six.
    assert all("5 commits" in rel.note for rel in co_changes), [r.note for r in co_changes]


def test_revert_outside_the_window_is_still_reported(git_repo) -> None:
    """Git's trailer states a fact; a shallow window must not erase it."""
    repo = git_repo({"src/a.py": "a = 1\n"})
    (repo / "src" / "a.py").write_text("a = 2\n", encoding="utf-8")
    run_git(repo, "add", "src/a.py")
    run_git(repo, "commit", "-m", "feat: risky change")
    for index in range(3):
        (repo / "src" / "noise.py").write_text(f"n = {index}\n", encoding="utf-8")
        run_git(repo, "add", "src/noise.py")
        run_git(repo, "commit", "-m", f"chore: noise {index}")
    run_git(repo, "revert", "--no-edit", "HEAD~3")

    evidence = mine_git_history(repo, max_commits=2)
    assert evidence is not None
    reverts = _of_kind(evidence, RELATIONSHIP_REVERT)
    assert len(reverts) == 1
    assert reverts[0].observed is True
    assert "outside the scanned window" in reverts[0].note


def test_hand_written_revert_without_trailer_is_not_asserted_as_a_link(git_repo) -> None:
    """A quoted subject cannot identify the reverted commit — no ``observed`` claim."""
    repo = git_repo({"src/a.py": "a = 1\n"})
    (repo / "src" / "a.py").write_text("a = 2\n", encoding="utf-8")
    run_git(repo, "add", "src/a.py")
    run_git(repo, "commit", "-m", "wip")
    (repo / "src" / "a.py").write_text("a = 3\n", encoding="utf-8")
    run_git(repo, "add", "src/a.py")
    run_git(repo, "commit", "-m", "wip")
    (repo / "src" / "a.py").write_text("a = 4\n", encoding="utf-8")
    run_git(repo, "add", "src/a.py")
    run_git(repo, "commit", "-m", 'Revert "wip"')

    evidence = mine_git_history(repo, max_commits=50)
    assert evidence is not None
    assert _of_kind(evidence, RELATIONSHIP_REVERT) == []


def test_test_paths_cover_common_layouts() -> None:
    assert _is_test_path("tests/test_models.py")
    assert _is_test_path("src/foo_test.py")
    assert _is_test_path("app/__tests__/x.spec.js")
    assert _is_test_path("src/foo.spec.ts")
    assert _is_test_path("pkg/module/test_inner.py")
    assert not _is_test_path("src/latest.py")
    assert not _is_test_path("src/greatest_hits.py")


def test_jest_fix_that_adds_tests_is_not_reported_as_untested(git_repo) -> None:
    """``__tests__`` must count as a test path, or the observed claim is a lie."""
    repo = git_repo({"app/math.js": "export const add = (a, b) => a - b;\n"})
    (repo / "app" / "__tests__").mkdir()
    (repo / "app" / "math.js").write_text("export const add = (a, b) => a + b;\n", encoding="utf-8")
    (repo / "app" / "__tests__" / "math.spec.js").write_text("test('add', () => {});\n", encoding="utf-8")
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-m", "fix: addition subtracted")

    evidence = mine_git_history(repo, max_commits=50)
    assert evidence is not None
    assert _of_kind(evidence, RELATIONSHIP_FIX_WITHOUT_TEST) == []


# --- schema -----------------------------------------------------------------


def test_evidence_block_round_trips_through_json_shape(git_repo) -> None:
    repo = git_repo(
        {"src/a.py": "a = 1\n"},
        later_commits=[("fix: correct a", {"src/a.py": "a = 2\n"})],
    )
    evidence = mine_git_history(repo, max_commits=50)
    assert evidence is not None

    block = evidence_to_block(evidence)
    restored = evidence_from_block(block)

    assert restored.commits_scanned == evidence.commits_scanned
    assert restored.hotspots == evidence.hotspots
    assert restored.relationships == evidence.relationships
    assert [c.sha for c in restored.commits] == [c.sha for c in evidence.commits]
    assert [c.excerpt for c in restored.commits] == [c.excerpt for c in evidence.commits]


def test_schema_one_block_still_loads_as_statistics() -> None:
    """A pre-upgrade evidence.json (statistics only) must not need a re-mine."""
    restored = evidence_from_block(
        {
            "commits_scanned": 42,
            "hotspots": [["src/a.py", 7]],
            "bug_classes": [{"keyword": "fix", "count": 3, "top_files": ["src/a.py"]}],
            "test_dirs": ["tests/"],
            "test_co_change_ratio": 0.25,
            "language_proportions": {"python": 100.0},
            "total_files_tracked": 9,
        }
    )
    assert isinstance(restored, GitEvidence)
    assert restored.commits_scanned == 42
    assert restored.hotspots == [("src/a.py", 7)]
    assert restored.relationships == []
    assert restored.commits == []
    assert restored.excerpts_enabled is False


def test_schema_one_payload_still_parses() -> None:
    restored = evidence_from_block({"commits_scanned": 1, "hotspots": [["x", 1]]})
    assert restored.commits_scanned == 1


def test_git_evidence_schema_is_two() -> None:
    assert GIT_EVIDENCE_SCHEMA == 2


def test_files_total_survives_a_round_trip() -> None:
    """A commit touching more files than the storage cap keeps its true count."""
    from spc.githistory import MAX_PERSISTED_FILES, CommitRecord, FileChange

    total = MAX_PERSISTED_FILES + 5
    record = CommitRecord(
        sha="a" * 40,
        subject="refactor: split the package",
        author_timestamp=1,
        committer_timestamp=1,
        files=tuple(
            FileChange(path=f"src/f{index:03d}.py", change_type="modified", added=1, deleted=1)
            for index in range(total)
        ),
    )
    evidence = GitEvidence(commits_scanned=1)
    evidence.commits = [record]

    block = evidence_to_block(evidence)
    assert block["commits"][0]["files_total"] == total
    assert len(block["commits"][0]["files"]) == MAX_PERSISTED_FILES

    restored = evidence_from_block(block)
    assert len(restored.commits[0].files) == MAX_PERSISTED_FILES
    assert restored.commits[0].files_total == total


def test_non_utf8_commit_message_does_not_discard_the_window(git_repo) -> None:
    """One latin-1 byte in a message must not make `spc learn` report "no history"."""
    repo = git_repo({"src/a.py": "a = 1\n"})
    run_git(repo, "config", "i18n.commitEncoding", "ISO-8859-1")
    (repo / "src" / "a.py").write_text("a = 2\n", encoding="utf-8")
    run_git(repo, "add", "src/a.py")
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-F", "-"],
        input="fix: caf\u00e9 bug\n".encode("latin-1"),
        capture_output=True,
        check=True,
    )

    evidence = mine_git_history(repo, max_commits=50)
    assert evidence is not None
    assert evidence.commits_scanned == 2
    assert any("caf" in commit.subject for commit in evidence.commits)


def test_profile_filter_catches_a_quote_from_a_late_excerpt() -> None:
    """The leak filter covers every excerpt, not the first 200 lines of them."""
    from spc.githistory import CommitRecord
    from spc.learn import _scrub_excerpt_leaks

    late_marker = "LATE_QUOTED_LINE_FROM_THE_LAST_EXCERPT = 41"
    commits = [
        CommitRecord(
            sha=f"{index:040x}",
            subject=f"fix: thing {index}",
            author_timestamp=0,
            committer_timestamp=0,
            excerpt="+UNIQUE_PADDING_LINE_" + f"{index:03d}" + "_ABCDEFGHIJ\n",
        )
        for index in range(300)
    ]
    commits.append(
        CommitRecord(
            sha="f" * 40,
            subject="fix: the last one",
            author_timestamp=0,
            committer_timestamp=0,
            excerpt=f"+{late_marker}\n",
        )
    )
    evidence = GitEvidence(commits_scanned=len(commits))
    evidence.commits = commits
    console = Console(file=StringIO())
    profile = f"## Conventions\n\n- {late_marker}\n- keep it tidy\n"
    assert late_marker not in _scrub_excerpt_leaks(profile, evidence, console)
    assert "keep it tidy" in _scrub_excerpt_leaks(profile, evidence, console)
