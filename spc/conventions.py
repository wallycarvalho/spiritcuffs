"""Discover repo convention files (CONTRIBUTING/AGENTS/lint configs) for the review prompt."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# Cap per convention file so one giant doc can't blow up the prompt.
MAX_CONVENTION_FILE_CHARS = 4000

CONVENTION_FILES = (
    "CONTRIBUTING.md",
    "AGENTS.md",
    ".editorconfig",
    ".ruff.toml",
    "ruff.toml",
    ".eslintrc",
    ".eslintrc.json",
    ".eslintrc.js",
    ".eslintrc.yml",
    "eslint.config.js",
    ".flake8",
    "setup.cfg",
)


@dataclass(frozen=True)
class ConventionFile:
    """One discovered convention file and its (truncated) content."""

    name: str
    content: str


def discover_conventions(repo_root: Path) -> list[ConventionFile]:
    """Find known convention files at the repo root and read their contents."""
    found: list[ConventionFile] = []
    for name in CONVENTION_FILES:
        path = repo_root / name
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        found.append(
            ConventionFile(name=name, content=text[:MAX_CONVENTION_FILE_CHARS])
        )
    return found
