"""Release-script unit tests: bump math, commit classification, changelog and
version-file editing. Pure functions only — no git, no network."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SCRIPT = importlib.util.spec_from_file_location(
    "release_script", Path(__file__).resolve().parent.parent / "scripts" / "release.py"
)
assert _SCRIPT and _SCRIPT.loader  # noqa: S101 — static file, always loadable
release = importlib.util.module_from_spec(_SCRIPT)
_SCRIPT.loader.exec_module(release)
_REAL_ENSURE_CLEAN_TREE = release.ensure_clean_tree

_FAKE_CHANGELOG = """\
# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.1] - 2026-11-16

### Fixed

- previous fix
"""

_FAKE_PYPROJECT = '[project]\nname = "spiritcuffs"\nversion = "0.1.1"\n'
_FAKE_INIT = '"""docstring."""\n\n__version__ = "0.1.1"\n'


def _point_main_at(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path, Path]:
    """Isolate main() from the real repo: fake file paths, no git checks."""
    pyproject = tmp_path / "pyproject.toml"
    init = tmp_path / "init.py"
    changelog = tmp_path / "CHANGELOG.md"
    pyproject.write_text(_FAKE_PYPROJECT, encoding="utf-8")
    init.write_text(_FAKE_INIT, encoding="utf-8")
    changelog.write_text(_FAKE_CHANGELOG, encoding="utf-8")
    monkeypatch.setattr(release, "PYPROJECT", pyproject)
    monkeypatch.setattr(release, "INIT_PY", init)
    monkeypatch.setattr(release, "CHANGELOG", changelog)
    for name in ("ensure_clean_tree", "ensure_on_main", "ensure_up_to_date"):
        monkeypatch.setattr(release, name, lambda _skip_fetch=False: None)
    return pyproject, init, changelog


# --- bump math ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("old", "level", "expected"),
    [
        ("0.1.1", "patch", "0.1.2"),
        ("0.1.1", "minor", "0.2.0"),
        ("0.1.1", "major", "1.0.0"),
        ("1.9.9", "patch", "1.9.10"),
        ("1.9.9", "minor", "1.10.0"),
        ("9.9.9", "major", "10.0.0"),
        ("2.0.0", "patch", "2.0.1"),
    ],
)
def test_bump_version_levels(old: str, level: str, expected: str) -> None:
    assert release.bump_version(old, level) == expected


@pytest.mark.parametrize("bad", ["1.2", "1.2.3.4", "1.2.x", "", "v1.2.3"])
def test_bump_version_rejects_non_semver(bad: str) -> None:
    with pytest.raises(release.ReleaseError, match="unsupported version format"):
        release.bump_version(bad, "patch")


def test_bump_version_rejects_unknown_level() -> None:
    with pytest.raises(release.ReleaseError, match="unknown bump level"):
        release.bump_version("0.1.1", "nano")  # type: ignore[arg-type]


# --- commit classification ----------------------------------------------------


def test_classify_known_prefixes() -> None:
    sections = release.classify_commits(
        ["feat: a", "feature: b", "fix: c", "hotfix: d", "perf: e", "docs: f"]
    )
    assert sections == {
        "Added": ["a", "b"],
        "Fixed": ["c", "d"],
        "Performance": ["e"],
        "Docs": ["f"],
    }


def test_classify_unknown_and_unprefixed_go_to_changed() -> None:
    sections = release.classify_commits(["weird: x", "just a subject", "random: y"])
    assert sections == {"Changed": ["weird: x", "just a subject", "random: y"]}


def test_classify_merge_commits_skipped() -> None:
    sections = release.classify_commits(
        ["Merge branch 'main'", "Merge pull request #1", "fix: real"]
    )
    assert sections == {"Fixed": ["real"]}
    # lowercase 'merge …' is not a merge-commit subject per the brief (exact
    # 'Merge ' prefix) — it lands in Changed verbatim
    assert release.classify_commits(["merge pull request"]) == {"Changed": ["merge pull request"]}


def test_classify_scoped_prefix_and_case() -> None:
    sections = release.classify_commits(["fix(npm): wrapper", "FIX: shouty"])
    assert sections == {"Fixed": ["wrapper", "shouty"]}


def test_classify_preserves_subject_verbatim_after_prefix() -> None:
    sections = release.classify_commits(["feat: Add Widget — keeps Proper Nouns!"])
    assert sections["Added"] == ["Add Widget — keeps Proper Nouns!"]


def test_classify_prefix_like_prose_is_not_a_prefix() -> None:
    sections = release.classify_commits(["note: this text mentions feat: later"])
    assert sections == {"Changed": ["note: this text mentions feat: later"]}


def test_classify_output_is_in_section_order() -> None:
    sections = release.classify_commits(["docs: d", "chore: c", "feat: a", "fix: f"])
    assert list(sections) == ["Added", "Fixed", "Changed", "Docs"]


# --- changelog rendering & insertion ------------------------------------------


def test_render_section_format() -> None:
    block = release.render_changelog_section("0.2.0", "2026-11-16", {"Added": ["new thing"]})
    assert block == "## [0.2.0] - 2026-11-16\n\n### Added\n\n- new thing"


def test_render_omits_empty_sections() -> None:
    block = release.render_changelog_section("0.2.0", "2026-11-16", {"Docs": ["only docs"]})
    assert "### Added" not in block
    assert block.endswith("### Docs\n\n- only docs")


def test_insert_keeps_intro_and_goes_above_old_sections() -> None:
    section = release.render_changelog_section(
        "0.2.0", "2026-11-16", {"Added": ["new thing"], "Fixed": ["bug gone"]}
    )
    updated = release.insert_changelog_section(_FAKE_CHANGELOG, section)
    assert updated.startswith(_FAKE_CHANGELOG[: _FAKE_CHANGELOG.index("## [0.1.1]")])
    assert updated.index("## [0.2.0]") < updated.index("## [0.1.1]")
    # exactly one blank line separates the new section from the old one
    assert "\n\n\n## [0.1.1]" not in updated
    assert updated.count("## [0.1.1]") == 1


def test_insert_after_intro_block_not_after_first_section() -> None:
    section = release.render_changelog_section("0.2.0", "2026-11-16", {"Docs": ["x"]})
    updated = release.insert_changelog_section(_FAKE_CHANGELOG, section)
    intro = "and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html)."
    after_intro = updated.split(intro, maxsplit=1)[1]
    assert after_intro.startswith("\n\n## [0.2.0] - 2026-11-16")
    # ...and the intro must appear exactly once (nothing was duplicated)
    assert updated.count(intro) == 1


def test_insert_missing_anchor_fails() -> None:
    with pytest.raises(release.ReleaseError, match="anchor"):
        release.insert_changelog_section("# Changelog\n\n## [0.1.0]\n", "## [0.2.0] - x")


def test_insert_missing_blank_line_after_anchor_fails() -> None:
    glued = _FAKE_CHANGELOG.replace("v2.0.0.html).\n\n", "v2.0.0.html).\n")
    with pytest.raises(release.ReleaseError, match="blank line"):
        release.insert_changelog_section(glued, "## [0.2.0] - x")


# --- version files ------------------------------------------------------------


def test_version_sync_across_both_files(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    init = tmp_path / "__init__.py"
    pyproject.write_text(_FAKE_PYPROJECT, encoding="utf-8")
    init.write_text(_FAKE_INIT, encoding="utf-8")
    new = release.bump_version(
        release.read_version(_FAKE_PYPROJECT, release._PYPROJECT_VERSION_RE, "py"), "patch"
    )
    pyproject.write_text(
        release.replace_version(_FAKE_PYPROJECT, release._PYPROJECT_VERSION_RE, new, "py"),
        encoding="utf-8",
    )
    init.write_text(
        release.replace_version(_FAKE_INIT, release._INIT_VERSION_RE, new, "init"),
        encoding="utf-8",
    )
    assert pyproject.read_text(encoding="utf-8") == _FAKE_PYPROJECT.replace("0.1.1", "0.1.2")
    assert init.read_text(encoding="utf-8") == _FAKE_INIT.replace("0.1.1", "0.1.2")


def test_version_patterns_preserve_surrounding_content() -> None:
    toml_with_comments = (
        "# a comment worth keeping\n"
        "[project]\n"
        'version = "0.1.1"  # trailing comment must survive\n'
        "name = 'x'\n"
    )
    bumped = release.replace_version(
        toml_with_comments, release._PYPROJECT_VERSION_RE, "0.1.2", "pyproject.toml"
    )
    assert "# a comment worth keeping" in bumped
    assert "# trailing comment must survive" in bumped


def test_duplicate_version_declarations_fail() -> None:
    doubled = _FAKE_PYPROJECT + 'version = "9.9.9"\n'
    with pytest.raises(release.ReleaseError, match="exactly one version declaration"):
        release.read_version(doubled, release._PYPROJECT_VERSION_RE, "py")


def test_missing_version_declaration_fails() -> None:
    with pytest.raises(release.ReleaseError, match="exactly one version declaration"):
        release.read_version("[project]\nname = 'x'\n", release._PYPROJECT_VERSION_RE, "py")


# --- dry-run / orchestration helpers ------------------------------------------


def test_dirty_entries_ignores_untracked() -> None:
    assert release.dirty_entries(" M uv.lock\n?? new-dir/\n") == [" M uv.lock"]
    assert release.dirty_entries("") == []
    assert release.dirty_entries("?? only-untracked\n") == []


def test_next_steps_match_brief() -> None:
    commands = release.next_steps("0.1.2")
    assert commands[0] == (
        'git add -A && git commit -m "chore(release): 0.1.2" && git tag v0.1.2 && git push --follow-tags'
    )
    assert commands[1] == "rm -rf dist && uv build && uv publish"


def test_main_dry_run_touches_nothing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    pyproject, init, changelog = _point_main_at(tmp_path, monkeypatch)
    monkeypatch.setattr(
        release, "commit_subjects_since_tag", lambda _current: ["feat: thing", "fix: other"]
    )
    monkeypatch.setattr(release, "today", lambda: "2026-11-16")

    exit_code = release.main(["patch", "--dry-run"])

    assert exit_code == 0
    assert pyproject.read_text(encoding="utf-8") == _FAKE_PYPROJECT
    assert init.read_text(encoding="utf-8") == _FAKE_INIT
    assert changelog.read_text(encoding="utf-8") == _FAKE_CHANGELOG


def test_main_real_run_updates_all_three_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pyproject, init, changelog = _point_main_at(tmp_path, monkeypatch)
    monkeypatch.setattr(release, "commit_subjects_since_tag", lambda _current: ["fix: only fix"])
    monkeypatch.setattr(release, "today", lambda: "2026-11-16")

    exit_code = release.main(["patch"])

    assert exit_code == 0
    assert 'version = "0.1.2"' in pyproject.read_text(encoding="utf-8")
    assert '__version__ = "0.1.2"' in init.read_text(encoding="utf-8")
    updated = changelog.read_text(encoding="utf-8")
    assert "## [0.1.2] - 2026-11-16" in updated
    assert updated.index("## [0.1.2]") < updated.index("## [0.1.1]")


def test_main_refuses_empty_commits(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _point_main_at(tmp_path, monkeypatch)
    monkeypatch.setattr(release, "commit_subjects_since_tag", lambda _current: [])

    assert release.main(["patch"]) == 1
    assert "nothing to release" in capsys.readouterr().err


def test_main_allow_empty_bumps_without_sections(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pyproject, init, changelog = _point_main_at(tmp_path, monkeypatch)
    monkeypatch.setattr(release, "commit_subjects_since_tag", lambda _current: [])
    monkeypatch.setattr(release, "today", lambda: "2026-11-16")

    assert release.main(["patch", "--allow-empty"]) == 0
    assert 'version = "0.1.2"' in pyproject.read_text(encoding="utf-8")
    assert '__version__ = "0.1.2"' in init.read_text(encoding="utf-8")
    # no sections → changelog untouched
    assert changelog.read_text(encoding="utf-8") == _FAKE_CHANGELOG
    assert "(none" in capsys.readouterr().out


def test_main_reports_dirty_tree(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _point_main_at(tmp_path, monkeypatch)
    monkeypatch.setattr(release, "ensure_clean_tree", _REAL_ENSURE_CLEAN_TREE)
    monkeypatch.setattr(release, "run_git", lambda *args: " M uv.lock\n")

    assert release.main(["patch"]) == 1
    assert "working tree not clean" in capsys.readouterr().err
