"""Profile staleness logic + evidence/profile persistence."""

from __future__ import annotations

import os
import time
from pathlib import Path

from spc.config import SpcConfig
from spc.githistory import GitEvidence, Relationship
from spc.profile import (
    artifacts_dir,
    evidence_path,
    load_evidence,
    profile_age_days,
    profile_path,
    save_evidence,
    save_profile,
    staleness_notice,
)


def _evidence() -> GitEvidence:
    return GitEvidence(
        commits_scanned=3,
        hotspots=[("src/a.py", 3)],
        bug_classes=[{"keyword": "fix", "count": 2, "top_files": ["src/a.py"]}],
        test_dirs=["tests/"],
        test_co_change_ratio=0.5,
        language_proportions={"python": 100.0},
        total_files_tracked=1,
    )


def test_save_and_load_evidence_roundtrip(tmp_path: Path) -> None:
    save_evidence(tmp_path, _evidence())
    loaded = load_evidence(tmp_path)
    assert loaded is not None
    assert loaded["commits_scanned"] == 3
    assert loaded["hotspots"] == [["src/a.py", 3]]
    assert evidence_path(tmp_path).is_file()
    assert (artifacts_dir(tmp_path) / "evidence.json").is_file()


def test_load_evidence_missing_or_corrupt(tmp_path: Path) -> None:
    assert load_evidence(tmp_path) is None
    artifacts_dir(tmp_path).mkdir()
    evidence_path(tmp_path).write_text("{not json", encoding="utf-8")
    assert load_evidence(tmp_path) is None


def test_profile_age_and_staleness_notice(tmp_path: Path) -> None:
    assert profile_age_days(tmp_path) is None
    assert staleness_notice(tmp_path, 30) is None  # no profile: no notice in review path

    save_profile(tmp_path, "# profile")
    assert profile_age_days(tmp_path) is not None
    assert profile_age_days(tmp_path) < 1
    assert staleness_notice(tmp_path, 30) is None

    # Backdate the file 40 days.
    old = time.time() - 40 * 86400
    os.utime(profile_path(tmp_path), (old, old))
    notice = staleness_notice(tmp_path, 30)
    assert notice is not None
    assert "40 days old" in notice
    assert "spc learn" in notice


def test_save_profile_creates_dir(tmp_path: Path) -> None:
    save_profile(tmp_path, "narrative")
    assert profile_path(tmp_path).read_text(encoding="utf-8") == "narrative"


def test_staleness_config_boundary(tmp_path: Path) -> None:
    save_profile(tmp_path, "# p")
    old = time.time() - 31 * 86400
    os.utime(profile_path(tmp_path), (old, old))
    config = SpcConfig()  # profile_max_age_days = 30
    notice = staleness_notice(tmp_path, config.learn.profile_max_age_days)
    assert notice is not None


def _evidence_with_excerpts(*excerpts: str) -> GitEvidence:
    from spc.githistory import CommitRecord

    return GitEvidence(
        commits_scanned=len(excerpts),
        excerpts_enabled=True,
        commits=[
            CommitRecord(
                sha=f"{index:040x}",
                subject=f"fix: thing {index}",
                author_timestamp=0,
                committer_timestamp=0,
                excerpt=excerpt,
            )
            for index, excerpt in enumerate(excerpts)
        ],
    )


def test_oversized_excerpt_does_not_hide_the_others_from_the_prompt() -> None:
    """One huge excerpt must be truncated, not abort the section.

    Previously the loop broke on the first block that did not fit the budget,
    so a single oversized excerpt silently removed every excerpt from the
    synthesis prompt for the whole repository.
    """
    from spc.profile import MAX_EXCERPT_PROMPT_CHARS, _render_excerpt_section

    huge = "-" + "x" * (MAX_EXCERPT_PROMPT_CHARS * 2)
    section = _render_excerpt_section(_evidence_with_excerpts(huge, "-SMALL_MARKER_LINE"))

    assert len(section) <= MAX_EXCERPT_PROMPT_CHARS + 200
    assert "…(truncated)" in section
    assert "-SMALL_MARKER_LINE" in section


def _relationship(
    kind: str, commits: tuple[str, ...], files: tuple[str, ...], *, observed: bool, note: str
) -> Relationship:
    return Relationship(kind=kind, observed=observed, commits=commits, files=files, note=note)


def _evidence_with_relationships(
    relationships: list[Relationship], subjects: dict[str, str]
) -> GitEvidence:
    """Evidence carrying subjects (for message lookup) and the given links."""
    from spc.githistory import CommitRecord

    return GitEvidence(
        commits_scanned=len(subjects),
        excerpts_enabled=False,
        commits=[
            CommitRecord(
                sha=sha,
                subject=subject,
                author_timestamp=0,
                committer_timestamp=0,
                files=tuple(),
            )
            for sha, subject in subjects.items()
        ],
        relationships=relationships,
    )


def test_blind_reverts_collapse_to_one_line() -> None:
    """42 file-less reverts spent over half the section budget saying nothing.

    Each carried only a SHA of a commit outside the window. They must fold into
    a single aggregate line so the informative events keep their budget.
    """
    from spc.profile import _render_relationship_section

    relationships = [
        _relationship("revert", (f"sha{index}",), tuple(), observed=True, note="")
        for index in range(40)
    ]
    relationships.append(
        _relationship("co_change", ("a", "b"), ("src/x.py", "src/y.py"), observed=True,
                      note="changed together in 9 commits")
    )
    evidence = _evidence_with_relationships(
        relationships, {"a": "subject a", "b": "subject b"}
    )
    section = _render_relationship_section(evidence)

    revert_lines = [line for line in section.splitlines() if "[observed] revert:" in line]
    assert len(revert_lines) == 1
    assert "40 further reverts" in revert_lines[0]
    assert "NOT evidence that reverts lack file data" in revert_lines[0]
    assert "src/x.py + src/y.py" in section


def test_co_change_line_names_the_file_pair() -> None:
    """The pair IS the finding; a line with only a commit count discards it."""
    from spc.profile import _render_relationship_section

    evidence = _evidence_with_relationships(
        [
            _relationship(
                "co_change",
                ("a",),
                ("setup.py", "src/transformers/dependency_versions_table.py"),
                observed=True,
                note="changed together in 20 commits",
            )
        ],
        {},
    )

    assert "setup.py + src/transformers/dependency_versions_table.py" in _render_relationship_section(
        evidence
    )


def test_cascade_section_reports_each_convergent_change() -> None:
    """Several fixes following ONE change is a claim git log alone cannot make cheap."""
    from spc.profile import _render_cascade_section

    evidence = _evidence_with_relationships(
        [
            _relationship("fix_after_change", ("f1", "parent"), ("src/a.py",), observed=False, note=""),
            _relationship("fix_after_change", ("f2", "parent"), ("tests/b.py",), observed=False, note=""),
            _relationship("fix_after_change", ("f3", "other"), ("src/c.py",), observed=False, note=""),
        ],
        {"parent": "Rename the cache parameter", "other": "tweak", "f1": "fix a", "f2": "fix b", "f3": "fix c"},
        )
    section = _render_cascade_section(evidence)

    assert "2 follow-up fixes after" in section
    assert "Rename the cache parameter" in section
    assert "src/a.py" in section and "tests/b.py" in section
    assert "1 follow-up" not in section


def test_cascade_parents_win_the_excerpt_budget() -> None:
    """Item 5: excerpts must document the cascade, not the revert cohort.

    Citation order is detector order, so every revert was cited before any fix;
    measured on a 2000-commit window, all 60 excerpt slots went to reverts and
    none to cascade commits.
    """
    from spc.githistory import _excerpt_priority

    priority = _excerpt_priority(
        [
            _relationship("revert", ("r1",), tuple(), observed=True, note=""),
            _relationship("fix_after_change", ("f1", "parent"), ("a",), observed=False, note=""),
            _relationship("fix_after_change", ("f2", "parent"), ("b",), observed=False, note=""),
            _relationship("fix_after_change", ("f3", "lonely"), ("c",), observed=False, note=""),
        ]
    )

    assert priority["parent"] == 0
    assert priority["f1"] == priority["f2"] == 1
    assert "f3" not in priority
    assert "r1" not in priority


def test_repeated_co_change_pairs_are_not_rendered_twice() -> None:
    """A pair seen in both directions must not consume two lines."""
    from spc.profile import _render_relationship_section

    evidence = _evidence_with_relationships(
        [
            _relationship("co_change", ("a",), ("src/a.py", "src/b.py"), observed=True, note="7 commits"),
            _relationship("co_change", ("b",), ("src/b.py", "src/a.py"), observed=True, note="7 commits"),
        ],
        {},
    )

    assert _render_relationship_section(evidence).count("co_change:") == 1


def test_observed_events_outrank_hypotheses_when_the_budget_is_short(monkeypatch) -> None:
    """Ranking hypotheses first made the model assert something false.

    A measured run ranked verbose ``fix_after_change`` hypotheses above observed
    reverts. The reverts never rendered, and the synthesis wrote "reverts have
    happened, but without file-specific details in the scanned window" — false,
    because fifteen reverts carried files. Facts must win the budget.
    """
    from spc import profile as profile_mod

    relationships = [
        _relationship(
            "fix_after_change",
            (f"f{index}", f"p{index}"),
            ("src/a.py",),
            observed=False,
            note=f'fix "subject {index}" follows "parent {index}" touching src/a.py',
        )
        for index in range(12)
    ]
    relationships.append(
        _relationship(
            "revert",
            ("r1",),
            ("src/transformers/cache_utils.py",),
            observed=True,
            note='revert of "Support per-layer cache configuration"',
        )
    )
    subjects = {f"f{i}": f"fix subject {i}" for i in range(12)}
    subjects.update({f"p{i}": f"parent subject {i}" for i in range(12)})
    subjects["r1"] = 'revert of "Support per-layer cache configuration"'
    evidence = _evidence_with_relationships(relationships, subjects)

    monkeypatch.setattr(profile_mod, "MAX_EVENTS_CHARS", 400)
    section = profile_mod._render_relationship_section(evidence)

    assert "cache_utils.py" in section, "the observed revert lost the budget to hypotheses"
    assert len(section) <= 400 + len("…(truncated)")
