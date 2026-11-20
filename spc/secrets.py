"""Local regex scan for obvious secret patterns in a diff. Cheap, noisy-safe, pre-LLM."""

from __future__ import annotations

import re
from collections.abc import Callable
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


_PRIVATE_KEY_BODY_CAP = 128

# A long base64 line is the shape of a PEM/PGP key body. Both letter cases are
# required so hex digests (sha256 constants, lockfile hashes) don't match.
_KEY_MATERIAL_LINE = re.compile(r"^(?=[A-Za-z0-9+/]{40,}={0,2}$)(?=.*[A-Z])(?=.*[a-z]).*$")

# A base64 run inside a longer line (``tls.key: LS0tLS1CRUdJTi…``).
_BASE64_RUN = re.compile(r"[A-Za-z0-9+/]{40,}={0,2}")

# base64("-----BEGIN"…) — the prefix every base64-wrapped PEM starts with.
# A k8s Secret / CI variable holding a TLS key is base64 of a PEM, so the value
# is the private key even though the line never says "PRIVATE KEY". Matching the
# encoded header is precise enough to run ungated: no ordinary base64 blob
# (lockfile hash, image fixture, UUID) begins with it.
_BASE64_PEM_PREFIX = "LS0tLS1CRUdJT"

# Files that plausibly hold key material — the only place the shape rule runs,
# because a 40-char CamelCase identifier is indistinguishable from base64.
_KEY_PATH_SUFFIXES = (
    ".pem",
    ".key",
    ".crt",
    ".p12",
    ".pfx",
    ".asc",
    ".gpg",
    ".ppk",
    "id_rsa",
    "id_ed25519",
    "id_ecdsa",
)
_KEY_PATH_SEGMENTS = frozenset({"keys", "secrets", "certs", "private", "credentials"})

# A real key body is many consecutive base64 lines; one long line is ambiguous.
MIN_KEY_BLOCK_LINES = 3


def is_diff_file_header(line: str) -> bool:
    """True for git's ``--- a/path`` / ``+++ b/path`` file-header lines.

    The trailing space matters: a removed line whose *content* starts with
    ``--`` (a PEM ``-----BEGIN`` marker, SQL comment, em-dash) renders as
    ``-------BEGIN…`` and must not be mistaken for a header.
    """
    return line.startswith(("--- ", "+++ "))


def is_change_line(line: str) -> bool:
    """True for an added/removed content line (excludes ``---``/``+++`` headers)."""
    return line.startswith(("+", "-")) and not is_diff_file_header(line)


def is_content_line(line: str) -> bool:
    """True for any diff line carrying file content — context included.

    Context matters for private keys: a rotation leaves ``-----BEGIN``
    unchanged, so the header appears as a context line, and the key material
    follows as changed lines. Scanning only ``+``/``-`` lines would miss the
    header and leave the body unredacted.
    """
    return line.startswith((" ", "+", "-")) and not is_diff_file_header(line)


def is_added_line(line: str) -> bool:
    """True for an added content line (the only side the push-path scan reads)."""
    return line.startswith("+") and not is_diff_file_header(line)


def _redact_line(lines: list[str], index: int) -> None:
    """Replace one added line's pattern-matched spans with markers (in place)."""
    line = lines[index]
    if not line.startswith("+") or is_diff_file_header(line):
        return
    lines[index] = "+" + _redact_spans(line[1:])


def _redact_key_body(
    lines: list[str], header_index: int, is_sensitive: Callable[[str], bool]
) -> None:
    """Redact the lines after a private-key header until ``-----END``.

    The base64 body matches no single-line pattern but IS the key material, so
    the whole block is replaced. *is_sensitive* selects which lines count (only
    added lines on the push path, every content line for historical excerpts —
    a rotation leaves some body lines unchanged between the two sides, and they
    are key material all the same). The cap keeps a missing END marker from
    swallowing an entire file section.
    """
    limit = min(header_index + 1 + _PRIVATE_KEY_BODY_CAP, len(lines))
    for index in range(header_index + 1, limit):
        line = lines[index]
        if "-----END" in line and "PRIVATE KEY-----" in line:
            return
        if is_sensitive(line):
            lines[index] = line[0] + "[REDACTED:private key body]"


def redact_secrets(diff_text: str, hits: list[SecretHit]) -> str:
    """Return *diff_text* with each detected secret span replaced by a marker.

    The LLM must never see the secret itself — only that one existed and
    what kind. Spans are located by re-running the patterns on the same
    lines the scan flagged (deterministic: same text → same redaction →
    same diff hash for caching).

    A private-key hit redacts the whole block: the base64 body lines match
    no pattern but ARE the key material.

    Text that no line-level rule can reach — a hunk header's context suffix,
    base64-wrapped PEM material — is scrubbed on every run, hits or not.
    """
    lines = diff_text.splitlines()
    flagged = {hit.line_number for hit in hits}
    for index in sorted(flagged):
        if 1 <= index <= len(lines):
            _redact_line(lines, index - 1)
    for hit in hits:
        if hit.label == "private key block" and 1 <= hit.line_number <= len(lines):
            _redact_key_body(lines, hit.line_number - 1, is_added_line)
    _scrub_always_visible_secrets(lines, is_added_line)
    return "\n".join(lines) + ("\n" if diff_text.endswith("\n") else "")


def _allowlisted(content: str) -> bool:
    """True when the line contains only a documentation example value.

    Keeps the well-known AWS docs credentials (used by every tutorial and
    by our own test fixtures) from triggering a forever-warning on every
    push — noise erodes trust in the real warnings.
    """
    return any(value in content for value in ALLOWLISTED_VALUES)


def _redact_base64_key_runs(content: str) -> str:
    """Replace base64-wrapped PEM material embedded in a longer line.

    ``  tls.key: LS0tLS1CRUdJTiBSU0EgUFJJVkFURSBLRVktLS0tLQ…`` is how a TLS
    key looks in a k8s Secret or a CI variable: base64 of a PEM, so no
    ``PRIVATE KEY`` marker and no chance for the whole-line shape rule. The
    encoded header is distinctive enough to act on without any path gating.
    """
    if _BASE64_PEM_PREFIX not in content:
        return content

    def replace(match: re.Match[str]) -> str:
        span = match.group(0)
        if span.startswith(_BASE64_PEM_PREFIX):
            return "[REDACTED:private key body]"
        return span

    return _BASE64_RUN.sub(replace, content)


def _scrub_always_visible_secrets(
    lines: list[str], is_sensitive: Callable[[str], bool]
) -> None:
    """Scrub secret-bearing text that no line-level rule reaches (in place).

    Two shapes are scrubbed on BOTH paths, ungated by file name, because the
    threat model is "this text is sent to an LLM": a hunk header's context
    suffix (git copies a neighbouring line there verbatim) and base64-wrapped
    PEM material (a TLS key in a k8s Secret is base64, not a ``PRIVATE KEY``
    marker). *is_sensitive* selects the diff sides — added lines on the push
    path, every content line for historical excerpts.
    """
    for index, line in enumerate(lines):
        if line.startswith("@@"):
            lines[index] = _redact_hunk_context(line)
            continue
        if not is_sensitive(line):
            continue
        content = line[1:]
        if _allowlisted(content):
            continue
        scrubbed = _redact_base64_key_runs(content)
        if scrubbed != content:
            lines[index] = line[0] + scrubbed


def scrub_diff_text(diff_text: str) -> str:
    """Redact secret-shaped spans on BOTH diff sides — for historical excerpts.

    The review path only flags *added* lines (a secret being introduced). A
    historical excerpt is different: it often shows a secret being *removed*
    by a later fix, and the value is just as sensitive on the ``-`` side.

    Private-key headers are located among ALL content lines, context included:
    a key rotation leaves ``-----BEGIN …`` unchanged, so it arrives as a
    context line while the key material arrives as ``-``/``+`` lines. Missing
    the header would leave the body — the actual key — untouched.
    """
    lines = diff_text.splitlines()
    header_indices: list[int] = []
    for index, line in enumerate(lines):
        if not is_content_line(line):
            continue
        content = line[1:]
        if _allowlisted(content):
            continue
        if _has_private_key_header(content):
            header_indices.append(index)
        if is_change_line(line):
            lines[index] = line[0] + _redact_spans(content)
    for header_index in header_indices:
        _redact_key_body(lines, header_index, is_content_line)
    _redact_headerless_key_material(lines)
    _scrub_always_visible_secrets(lines, is_content_line)
    return "\n".join(lines) + ("\n" if diff_text.endswith("\n") else "")


def _redact_headerless_key_material(lines: list[str]) -> None:
    """Redact base64 key material whose ``-----BEGIN`` header is out of view.

    Rotating a key in the middle of a file leaves the header far outside the
    diff's context window, so the body lines arrive with nothing to anchor
    them. They are still key material.

    Shape alone is not enough to act on: a 40-character CamelCase identifier is
    indistinguishable from a base64 line, and redacting those would quietly
    destroy the evidence this feature exists to collect. So the shape rule
    applies only inside a file section that plausibly holds a key — a key-ish
    path, or a run of consecutive base64 lines where a real key body sits.

    Excerpt path only, deliberately: the push path's contract is "added lines
    matching a known pattern". A shape rule there would fire on every base64
    fixture in a test suite, and that noise teaches people to ignore the
    warning.
    """
    for path, indices in _diff_sections(lines):
        if not _section_may_hold_key_material(path, lines, indices):
            continue
        for index in indices:
            line = lines[index]
            if (
                is_change_line(line)
                and not _allowlisted(line[1:])
                and _looks_like_key_material(line[1:])
            ):
                lines[index] = line[0] + "[REDACTED:private key body]"


def _diff_sections(lines: list[str]) -> list[tuple[str, list[int]]]:
    """Split diff lines into ``(path, line indices)`` sections, one per file."""
    sections: list[tuple[str, list[int]]] = []
    path = ""
    indices: list[int] = []
    for index, line in enumerate(lines):
        if line.startswith("diff --git "):
            if indices:
                sections.append((path, indices))
            path = _section_path(line)
            indices = []
            continue
        indices.append(index)
    if indices:
        sections.append((path, indices))
    return sections


def _section_path(diff_header: str) -> str:
    """Extract the post-image path from a ``diff --git a/x b/y`` line."""
    _, _, tail = diff_header.partition(" b/")
    return tail if tail else diff_header


def _section_may_hold_key_material(path: str, lines: list[str], indices: list[int]) -> bool:
    """True when this file section plausibly contains key material."""
    lowered = path.lower()
    if lowered.endswith(_KEY_PATH_SUFFIXES):
        return True
    if _KEY_PATH_SEGMENTS & set(lowered.split("/")[:-1]):
        return True
    run = 0
    for index in indices:
        line = lines[index]
        if is_content_line(line) and _looks_like_key_material(line[1:]):
            run += 1
            if run >= MIN_KEY_BLOCK_LINES:
                return True
        elif not line.startswith("@@"):
            run = 0
    return False


def _redact_hunk_context(line: str) -> str:
    """Scrub a hunk header's context suffix — git copies a neighbour line there.

    ``@@ -20,3 +20,3 @@ <neighbouring line>`` repeats a nearby line verbatim
    (git's "function context"), and that line never appears as a body line, so
    no line-level rule sees it. Both pattern-shaped secrets (an AWS key, a
    token) and key material are scrubbed here, in every file — a secret does
    not become harmless because the file is called ``config.py``.
    """
    marker = line.find("@@", 2)
    if marker == -1:
        return line
    prefix, suffix = line[: marker + 2], line[marker + 2 :].strip()
    if not suffix:
        return line
    scrubbed = _redact_spans(suffix)
    if _looks_like_key_material(scrubbed) or _BASE64_PEM_PREFIX in scrubbed:
        scrubbed = "[REDACTED:private key body]"
    return f"{prefix} {scrubbed}" if scrubbed != suffix else line


def _has_private_key_header(content: str) -> bool:
    """True when the line opens a private-key block."""
    return any(
        label == "private key block" and pattern.search(content)
        for label, pattern in SECRET_PATTERNS
    )


def _looks_like_key_material(content: str) -> bool:
    """True for a long base64 line — the shape of a PEM/PGP key body.

    Needed because a header is not always visible: rotating a key in the middle
    of a file leaves ``-----BEGIN`` far outside the diff's context window, so
    the body arrives with nothing to anchor it, and base64 matches no
    single-line secret pattern. Requiring both letter cases keeps hex digests
    (sha256 constants, lockfile hashes) out of this rule.
    """
    return bool(_KEY_MATERIAL_LINE.match(content))


def _redact_spans(content: str) -> str:
    """Replace every secret-shaped span in one line's content with a marker."""
    for label, pattern in SECRET_PATTERNS:
        content = pattern.sub(f"[REDACTED:{label}]", content)
    return content


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
        if not in_hunk or not is_added_line(line):
            continue
        content = line[1:]
        if _allowlisted(content):
            continue
        for label, pattern in SECRET_PATTERNS:
            if pattern.search(content) and (label, index) not in seen:
                hits.append(SecretHit(label=label, line_number=index))
                seen.add((label, index))
    return hits
