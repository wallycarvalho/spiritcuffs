"""Local regex scan for obvious secret patterns in a diff. Cheap, noisy-safe, pre-LLM."""

from __future__ import annotations

import re
from dataclasses import dataclass

# Only added lines count (+ lines), so pre-existing secrets don't re-alarm forever.
# Each pattern: (label, compiled regex over one line).
SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("AWS access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    (
        "AWS secret in assignment",
        re.compile(r"(?i)aws_secret_access_key\s*=\s*['\"]?[A-Za-z0-9/+=]{20,}"),
    ),
    (
        "private key block",
        re.compile(
            r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY(?: BLOCK)?-----"
        ),
    ),
    ("generic high-entropy assignment", re.compile(
        r"(?i)\b(api[_-]?key|api[_-]?secret|secret|token|passwd|password|pwd)"
        r"\s*[=:]\s*['\"][A-Za-z0-9_\-/+=.]{16,}['\"]"
    )),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("Slack token", re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}\b")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("Stripe key", re.compile(r"\b(sk|rk)_(?:test|live)_[0-9A-Za-z]{16,}\b")),
    ("JWT", re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b")),
)


@dataclass(frozen=True)
class SecretHit:
    """One suspected secret found in the diff."""

    label: str
    line_number: int  # 1-based within the diff text


def scan_diff_for_secrets(diff_text: str) -> list[SecretHit]:
    """Scan *added* diff lines for secret-shaped strings; return hits (deduped by label+line)."""
    hits: list[SecretHit] = []
    seen: set[tuple[str, int]] = set()
    in_hunk = False
    for index, line in enumerate(diff_text.splitlines(), start=1):
        if line.startswith("@@"):
            in_hunk = True
            continue
        if line.startswith("diff --git ") or line.startswith("index "):
            in_hunk = False
            continue
        if not in_hunk or not line.startswith("+") or line.startswith("+++"):
            continue
        content = line[1:]
        for label, pattern in SECRET_PATTERNS:
            if pattern.search(content) and (label, index) not in seen:
                hits.append(SecretHit(label=label, line_number=index))
                seen.add((label, index))
    return hits
