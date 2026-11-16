#!/usr/bin/env python3
"""Release automation for SpiritCuffs.

Bumps the version (pyproject.toml + spc/__init__.py), rewrites CHANGELOG.md from
conventional-commit subjects, and prints the exact commands for the human to
run. It never commits, tags, pushes, builds, or publishes — the changelog is
meant to be eyeballed before it becomes history.

Usage:
    python scripts/release.py {major|minor|patch} [--dry-run] [--allow-empty] [--skip-fetch]
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = REPO_ROOT / "pyproject.toml"
INIT_PY = REPO_ROOT / "spc" / "__init__.py"
CHANGELOG = REPO_ROOT / "CHANGELOG.md"

# The intro block ends with this line; new sections are inserted right after it
# (plus one blank line), so they sit above every existing version section.
CHANGELOG_ANCHOR = (
    "and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html)."
)
SECTION_ORDER = ("Added", "Fixed", "Changed", "Performance", "Docs")

# Conventional-commit type → changelog section. Unknown types and un-prefixed
# subjects fall back to Changed (see classify_commits).
PREFIX_SECTIONS = {
    "feat": "Added",
    "feature": "Added",
    "fix": "Fixed",
    "hotfix": "Fixed",
    "perf": "Performance",
    "docs": "Docs",
    "chore": "Changed",
    "refactor": "Changed",
    "test": "Changed",
    "ci": "Changed",
    "build": "Changed",
}

# Matches "type:" and "type(scope):" at the start of a subject — never prose
# that merely contains a colon further in.
_PREFIX_RE = re.compile(r"^(?P<type>[A-Za-z]+)(?:\([^)]*\))?:\s*")
# Matches `version = "X.Y.Z"` — with or without a trailing comment — capturing
# only the quoted value so replacement preserves any comment on the line.
_PYPROJECT_VERSION_RE = re.compile(
    r'^version = "(?P<version>[^"]*)"(?P<trailing>\s+#.*)?$', re.MULTILINE
)
# `__version__` lines have no trailing-comment convention in this repo; keep the
# strict form so a stray lookalike can't be silently rewritten.
_INIT_VERSION_RE = re.compile(r'^__version__ = "(?P<version>[^"]*)"$', re.MULTILINE)


class ReleaseError(Exception):
    """Fatal, user-facing problem; printed as a single line, exit code 1."""


def bump_version(version: str, level: str) -> str:
    """Return *version* bumped by *level* ("major", "minor", or "patch")."""
    parts = version.split(".")
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        raise ReleaseError(f"unsupported version format: {version!r} (expected X.Y.Z)")
    major, minor, patch = (int(part) for part in parts)
    if level == "patch":
        patch += 1
    elif level == "minor":
        minor, patch = minor + 1, 0
    elif level == "major":
        major, minor, patch = major + 1, 0, 0
    else:
        raise ReleaseError(f"unknown bump level: {level!r}")
    return f"{major}.{minor}.{patch}"


def classify_commits(subjects: list[str]) -> dict[str, list[str]]:
    """Group commit subjects into changelog sections.

    Merge commits are skipped; every other subject loses its conventional-commit
    prefix (kept verbatim otherwise) and lands in its section, or in Changed for
    unknown/un-prefixed subjects. Empty sections are omitted; the rest keep the
    canonical SECTION_ORDER.
    """
    sections: dict[str, list[str]] = {}
    for subject in subjects:
        stripped = subject.strip()
        if not stripped or stripped.startswith("Merge "):
            continue
        match = _PREFIX_RE.match(stripped)
        section = PREFIX_SECTIONS.get(match.group("type").lower()) if match else None
        if section is not None:
            entry = stripped[match.end() :].strip()  # type: ignore[union-attr]
        else:
            # Unknown type or un-prefixed prose: keep the subject verbatim.
            section, entry = "Changed", stripped
        if entry:
            sections.setdefault(section, []).append(entry)
    return {name: sections[name] for name in SECTION_ORDER if name in sections}


def render_changelog_section(version: str, today: str, sections: dict[str, list[str]]) -> str:
    """Render the `## [version] - date` block for *sections* (no trailing newline)."""
    lines = [f"## [{version}] - {today}", ""]
    for name in SECTION_ORDER:
        entries = sections.get(name)
        if not entries:
            continue
        lines += [f"### {name}", "", *(f"- {entry}" for entry in entries), ""]
    return "\n".join(lines).rstrip("\n")


def insert_changelog_section(changelog: str, section: str) -> str:
    """Insert *section* right after the fixed intro header (anchor line + blank line)."""
    anchor = changelog.find(CHANGELOG_ANCHOR)
    if anchor == -1:
        raise ReleaseError("CHANGELOG.md: intro anchor (Semantic Versioning line) not found")
    line_end = changelog.find("\n", anchor)
    rest = changelog[line_end + 1 :] if line_end != -1 else ""
    if not rest.startswith("\n"):
        raise ReleaseError("CHANGELOG.md: expected a blank line after the intro anchor")
    head = changelog[: line_end + 1]
    return f"{head}\n{section}\n\n{rest[1:]}"


def _sole_version_match(text: str, pattern: re.Pattern[str], source: str) -> re.Match[str]:
    matches = list(pattern.finditer(text))
    if len(matches) != 1:
        raise ReleaseError(
            f"{source}: expected exactly one version declaration, found {len(matches)}"
        )
    return matches[0]


def read_version(text: str, pattern: re.Pattern[str], source: str) -> str:
    return _sole_version_match(text, pattern, source).group("version")


def replace_version(text: str, pattern: re.Pattern[str], new_version: str, source: str) -> str:
    """Rewrite the version declaration via targeted regex — a TOML round-trip
    would destroy comments (the .spc.toml lesson)."""
    match = _sole_version_match(text, pattern, source)
    start, end = match.span("version")
    return text[:start] + new_version + text[end:]


def dirty_entries(porcelain: str) -> list[str]:
    """Modified/staged paths from `git status --porcelain`; untracked (??) is allowed."""
    return [
        line for line in porcelain.splitlines() if line.strip() and not line.startswith("?? ")
    ]


def run_git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise ReleaseError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def ensure_clean_tree() -> None:
    dirty = dirty_entries(run_git("status", "--porcelain"))
    if dirty:
        raise ReleaseError(f"working tree not clean (commit or stash first): {'; '.join(dirty)}")


def ensure_on_main() -> None:
    branch = run_git("branch", "--show-current")
    if branch != "main":
        raise ReleaseError(f"releases run from main; current branch is {branch!r}")


def ensure_up_to_date(skip_fetch: bool) -> None:
    if not skip_fetch:
        try:
            run_git("fetch", "origin")
        except ReleaseError as exc:
            raise ReleaseError(f"{exc} (offline? retry with --skip-fetch)") from None
    local = run_git("rev-parse", "HEAD")
    remote = run_git("rev-parse", "--verify", "origin/main")
    if local != remote:
        raise ReleaseError("main is not in sync with origin/main; pull or push first")


def commit_subjects_since_tag(current_version: str) -> list[str]:
    """Subjects of every commit after the most recent tag (or tag `v<current>`)."""
    tag: str | None = None
    try:
        tag = run_git("describe", "--tags", "--abbrev=0")
    except ReleaseError:
        pass  # no tags at all — fall back to the v<current> ref below
    if tag:
        return _log_subjects(f"{tag}..HEAD")
    try:
        run_git("rev-parse", "--verify", f"v{current_version}")
    except ReleaseError:
        raise ReleaseError(
            f"no git tag to diff against (tried --tags and v{current_version}); "
            "tag the previous release first"
        ) from None
    return _log_subjects(f"v{current_version}..HEAD")


def _log_subjects(range_spec: str) -> list[str]:
    output = run_git("log", range_spec, "--format=%s")
    return [line for line in output.splitlines() if line.strip()]


def next_steps(new_version: str) -> list[str]:
    return [
        f'git add -A && git commit -m "chore(release): {new_version}" '
        f'&& git tag v{new_version} && git push --follow-tags',
        "uv build && uv publish",
    ]


def today() -> str:
    return date.today().isoformat()


def print_report(
    old: str,
    new: str,
    sections: dict[str, list[str]],
    dry_run: bool,
    changelog_changed: bool,
) -> None:
    if dry_run:
        print("DRY RUN — no files were modified.")
    print(f"\nRelease {old} → {new}")
    section_summary = ", ".join(f"{name} ({len(entries)})" for name, entries in sections.items())
    print(f"Sections: {section_summary or '(none — CHANGELOG.md left unchanged)'}")
    print(f"Files {'to touch' if dry_run else 'touched'}:")
    if changelog_changed:
        print("  - CHANGELOG.md (new section above the previous ones)")
    else:
        print("  - CHANGELOG.md (unchanged)")
    print(f"  - pyproject.toml (version {old} → {new})")
    print(f"  - spc/__init__.py (__version__ {old} → {new})")
    print("\nNext steps (eyeball CHANGELOG.md first, then):")
    for command in next_steps(new):
        print(f"    {command}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="release.py",
        description=(
            "Bump the version and rewrite CHANGELOG.md, then print the "
            "commit/tag/publish commands. Never commits, tags, or publishes itself."
        ),
    )
    parser.add_argument("level", choices=("major", "minor", "patch"))
    parser.add_argument("--dry-run", action="store_true", help="print the plan, touch no files")
    parser.add_argument(
        "--allow-empty",
        action="store_true",
        help="release even with no new commits since the last tag",
    )
    parser.add_argument("--skip-fetch", action="store_true", help="skip `git fetch` (offline runs)")
    args = parser.parse_args(argv)

    try:
        ensure_clean_tree()
        ensure_on_main()
        ensure_up_to_date(args.skip_fetch)

        pyproject = PYPROJECT.read_text(encoding="utf-8")
        current = read_version(pyproject, _PYPROJECT_VERSION_RE, "pyproject.toml")
        new_version = bump_version(current, args.level)

        subjects = commit_subjects_since_tag(current)
        if not subjects and not args.allow_empty:
            raise ReleaseError(
                "nothing to release: no commits since last tag (use --allow-empty to override)"
            )
        sections = classify_commits(subjects)
        if not sections and not args.allow_empty:
            raise ReleaseError(
                "nothing to release: no release-worthy commits since last tag (use --allow-empty)"
            )

        changelog = CHANGELOG.read_text(encoding="utf-8")
        new_changelog = (
            insert_changelog_section(
                changelog, render_changelog_section(new_version, today(), sections)
            )
            if sections
            else changelog
        )
        new_pyproject = replace_version(
            pyproject, _PYPROJECT_VERSION_RE, new_version, "pyproject.toml"
        )
        init = INIT_PY.read_text(encoding="utf-8")
        new_init = replace_version(init, _INIT_VERSION_RE, new_version, "spc/__init__.py")
        # The missed-bump bug (f727a98) is why this check exists.
        if any(
            read_version(text, pattern, source) != new_version
            for text, pattern, source in (
                (new_pyproject, _PYPROJECT_VERSION_RE, "pyproject.toml"),
                (new_init, _INIT_VERSION_RE, "spc/__init__.py"),
            )
        ):
            raise ReleaseError("internal error: version files disagree after the bump")

        changelog_changed = new_changelog != changelog
        if args.dry_run:
            print_report(
                current, new_version, sections, dry_run=True, changelog_changed=changelog_changed
            )
            if sections:
                print("\nCHANGELOG.md — section that would be inserted:\n")
                print(render_changelog_section(new_version, today(), sections))
        else:
            if changelog_changed:
                CHANGELOG.write_text(new_changelog, encoding="utf-8")
            PYPROJECT.write_text(new_pyproject, encoding="utf-8")
            INIT_PY.write_text(new_init, encoding="utf-8")
            print_report(
                current, new_version, sections, dry_run=False, changelog_changed=changelog_changed
            )
    except (ReleaseError, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
