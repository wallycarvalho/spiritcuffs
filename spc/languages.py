"""File-extension → language tags and short per-language review hints."""

from __future__ import annotations

# Short "what to watch for" hints injected into the review prompt.
LANGUAGE_HINTS: dict[str, str] = {
    "python": "mutable default arguments, bare except, subprocess shell=True, "
    "unsafe deserialization",
    "javascript": "== vs ===, async/await error swallowing, prototype pollution, "
    "unbounded fetch",
    "typescript": "type narrowing gaps, any-casts, unhandled promise rejections",
    "go": "ignored error returns, goroutine leaks, data races on shared maps",
    "rust": "unwrap/expect on fallible paths, unsafe blocks, integer overflow in "
    "release mode",
    "java": "resource leaks without try-with-resources, equals/hashCode mismatch",
    "ruby": "monkey patching, mass assignment, unbounded ActiveRecord queries",
    "shell": "unquoted variable expansion, rm -rf on unvalidated paths, missing set -e",
    "sql": "string-concatenated queries (injection), missing LIMIT on scans",
    "yaml": "indentation traps, unvalidated interpolation from secrets/env",
    "json": "trailing commas, unvalidated shape assumptions",
    "html": "unescaped interpolation (XSS), missing CSP hints",
    "css": "specificity wars, layout thrash via layout-triggering properties",
}

EXTENSION_LANGUAGE: dict[str, str] = {
    ".py": "python",
    ".pyi": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".rb": "ruby",
    ".sh": "shell",
    ".bash": "shell",
    ".zsh": "shell",
    ".sql": "sql",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".json": "json",
    ".html": "html",
    ".css": "css",
}


def language_for_file(name: str) -> str | None:
    """Map a filename to its language tag, or None for unknown extensions."""
    suffix = name.rsplit(".", 1)[-1] if "." in name else ""
    dot_name = f".{suffix}" if suffix else ""
    return EXTENSION_LANGUAGE.get(dot_name)


def hint_for_language(language: str) -> str | None:
    """Return the short review hint for *language*, or None."""
    return LANGUAGE_HINTS.get(language)


def language_proportions(files: list[str]) -> dict[str, float]:
    """Rough language proportions over *files* (by extension count).

    Files with unknown extensions are grouped under "other". Returns percentages
    summing to ~100 (0.0 when *files* is empty).
    """
    if not files:
        return {}
    counts: dict[str, int] = {}
    for name in files:
        language = language_for_file(name) or "other"
        counts[language] = counts.get(language, 0) + 1
    total = sum(counts.values())
    return {language: round(100 * count / total, 1) for language, count in counts.items()}
