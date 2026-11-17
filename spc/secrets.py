"""Local regex scan for obvious secret patterns in a diff. Cheap, noisy-safe, pre-LLM."""

from __future__ import annotations

import re
from dataclasses import dataclass

# Well-known DOCUMENTATION example values — not real secrets. Flagging them
# would make every tutorial-following repo flag forever on every push, which
# erodes trust in the warning itself (GH's secret scanning does the same).
ALLOWLISTED_VALUES: tuple[str, ...] = (
    "AKIAIOSFODNN7EXAMPLE",  # AWS docs example access key ID
    "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",  # AWS docs example secret key
)

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


_PRIVATE_KEY_BODY_CAP = 40


def _redact_line(lines: list[str], index: int) -> None:
    """Replace one added line's pattern-matched spans with markers (in place)."""
    line = lines[index]
    if not line.startswith("+") or line.startswith("+++"):
        return
    content = line[1:]
    for label, pattern in SECRET_PATTERNS:
        content = pattern.sub(f"[REDACTED:{label}]", content)
    lines[index] = "+" + content


def _redact_private_key_body(lines: list[str], header_index: int) -> None:
    """Redact every added line after a private-key header until ``-----END``.

    The base64 body matches no single-line pattern but IS the key material,
    so the whole block is replaced. The cap keeps a missing END marker from
    swallowing an entire file section.
    """
    limit = min(header_index + _PRIVATE_KEY_BODY_CAP, len(lines))
    # header_index is 1-based → body starts at lines[header_index].
    for index in range(header_index, limit):
        line = lines[index]
        if "-----END" in line and "PRIVATE KEY-----" in line:
            break
        if line.startswith("+") and not line.startswith("+++"):
            lines[index] = "+[REDACTED:private key body]"


def redact_secrets(diff_text: str, hits: list[SecretHit]) -> str:
    """Return *diff_text* with each detected secret span replaced by a marker.

    The LLM must never see the secret itself — only that one existed and
    what kind. Spans are located by re-running the patterns on the same
    lines the scan flagged (deterministic: same text → same redaction →
    same diff hash for caching).

    A private-key hit redacts the whole block: the base64 body lines match
    no pattern but ARE the key material.
    """
    if not hits:
        return diff_text
    lines = diff_text.splitlines()
    flagged = {hit.line_number for hit in hits}
    for index in sorted(flagged):
        if 1 <= index <= len(lines):
            _redact_line(lines, index - 1)
    for hit in hits:
        if hit.label == "private key block" and 1 <= hit.line_number <= len(lines):
            _redact_private_key_body(lines, hit.line_number)
    return "\n".join(lines) + ("\n" if diff_text.endswith("\n") else "")


def _allowlisted(content: str) -> bool:
    """True when the line contains only a documentation example value.

    Keeps the well-known AWS docs credentials (used by every tutorial and
    by our own test fixtures) from triggering a forever-warning on every
    push — noise erodes trust in the real warnings.
    """
    return any(value in content for value in ALLOWLISTED_VALUES)


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
        if _allowlisted(content):
            continue
        for label, pattern in SECRET_PATTERNS:
            if pattern.search(content) and (label, index) not in seen:
                hits.append(SecretHit(label=label, line_number=index))
                seen.add((label, index))
    return hits
