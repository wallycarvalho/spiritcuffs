"""Secrets regex scan tests."""

from __future__ import annotations

from conftest import AWS_FIXTURE_KEY, PRIVATE_KEY_FOOTER, PRIVATE_KEY_HEADER

from spc.secrets import (
    _PRIVATE_KEY_BODY_CAP,
    ALLOWLISTED_VALUES,
    redact_secrets,
    scan_diff_for_secrets,
    scrub_diff_text,
)

# Built at runtime so no matchable secret literal lives in the tree (GitHub's
# push protection blocks on them, and so does our own scanner).
DIFF_WITH_SECRETS = (
    "diff --git a/config.py b/config.py\n"
    "index 111..222 100644\n"
    "--- a/config.py\n"
    "+++ b/config.py\n"
    "@@ -1,3 +1,5 @@\n"
    " import os\n"
    f'+AWS_KEY = "{AWS_FIXTURE_KEY}"\n'
    "+token = \"ghp_" + "A" * 36 + "\"\n"
    '+password = "super-secret-password-123"\n'
    " def main():\n"
)


def test_detects_aws_key_and_github_token() -> None:
    hits = scan_diff_for_secrets(DIFF_WITH_SECRETS)
    labels = {hit.label for hit in hits}
    assert "AWS access key" in labels
    assert "GitHub token" in labels
    assert "generic high-entropy assignment" in labels


def test_ignores_context_lines_and_existing_secrets() -> None:
    diff = (
        "diff --git a/a.py b/a.py\n"
        "--- a/a.py\n"
        "+++ b/a.py\n"
        "@@ -1,2 +1,2 @@\n"
        f"-AWS_KEY = '{AWS_FIXTURE_KEY}'\n"  # removed line — not a new leak
        "+AWS_KEY = os.environ['AWS_KEY']\n"  # safe replacement
    )
    assert scan_diff_for_secrets(diff) == []


def test_ignores_short_generic_values() -> None:
    diff = (
        "diff --git a/a.py b/a.py\n"
        "--- a/a.py\n"
        "+++ b/a.py\n"
        "@@ -1 +1,2 @@\n"
        "+token = \"abc\"\n"  # too short to be a secret
        "+timeout = 30\n"
    )
    assert scan_diff_for_secrets(diff) == []


def test_allowlisted_documentation_values_are_ignored() -> None:
    """The AWS docs example key must not flag — every tutorial repo would otherwise
    warn forever, and our own fixtures use it (real push caught this)."""
    diff = (
        "diff --git a/tests/test_x.py b/tests/test_x.py\n"
        "--- a/tests/test_x.py\n"
        "+++ b/tests/test_x.py\n"
        "@@ -0,0 +1,2 @@\n"
        f"+SECRET_LINE = 'AWS_KEY = \"{ALLOWLISTED_VALUES[0]}\"'\n"
        "+secret_key = \"wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY\"\n"
    )
    assert scan_diff_for_secrets(diff) == []


def test_allowlist_still_flags_real_aws_keys() -> None:
    """The allowlist matches exact doc values only — a real-shaped key still flags."""
    diff = (
        "diff --git a/prod.py b/prod.py\n"
        "--- a/prod.py\n"
        "+++ b/prod.py\n"
        "@@ -0,0 +1,1 @@\n"
        f'+KEY = "{AWS_FIXTURE_KEY[:-1]}Z"\n'  # one char off the docs value
    )
    hits = scan_diff_for_secrets(diff)
    assert [hit.label for hit in hits] == ["AWS access key"]


def test_detects_private_key_and_jwt() -> None:
    jwt = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9P" "l_5f6K2Xm7s"
    diff = (
        "diff --git a/keys b/keys\n"
        "--- a/keys\n"
        "+++ b/keys\n"
        "@@ -0,0 +1,2 @@\n"
        f"+{PRIVATE_KEY_HEADER}\n"
        f"+Authorization: Bearer {jwt}\n"
    )
    labels = {hit.label for hit in scan_diff_for_secrets(diff)}
    assert "private key block" in labels
    assert "JWT" in labels


def test_private_key_body_lines_are_redacted_too() -> None:
    """Blocker regression: the base64 body IS the key material.

    The header line matches the pattern; the body lines match nothing —
    but they must not reach the LLM either. Redaction covers the whole
    block through the END marker.
    """
    diff = (
        "diff --git a/server.pem b/server.pem\n"
        "--- /dev/null\n"
        "+++ b/server.pem\n"
        "@@ -0,0 +1,7 @@\n"
        f"+{PRIVATE_KEY_HEADER}\n"
        "+MIIEpAIBAAKCAQEA7dE8wK8xExample0123456789abcdefgh\n"
        "+hqLvG2xJ9pQ0R3tU5vW7yZ9aB1cD3eF5gH7iJ9kL1mN3oP5q\n"
        "+rS7tU9vW1xY3zA5bC7dE9fG1hI3jK5lM7nO9pQ1rS3tU5v\n"
        "+wX7yZ9aB1cD3eF5gH7iJ9kL1mN3oP5qR7tU9vW1xY3zA5b\n"
        "+C7dE9fG1hI3jK5lM7nO9pQ1rS3tU5vW7yZ9aB1cD3eF5gH\n"
        f"+{PRIVATE_KEY_FOOTER}\n"
    )
    hits = scan_diff_for_secrets(diff)
    assert [hit.label for hit in hits] == ["private key block"]
    redacted = redact_secrets(diff, hits)
    assert "MIIEpAIBAAKCAQEA" not in redacted
    assert "hqLvG2xJ9pQ0R3tU5vW7yZ9aB1cD3eF5gH" not in redacted
    assert "[REDACTED:private key block]" in redacted
    assert redacted.count("[REDACTED:private key body]") == 5
    assert PRIVATE_KEY_FOOTER in redacted  # END marker survives


def test_private_key_body_cap_bounds_redaction() -> None:
    """A missing END marker must not swallow the rest of the diff."""
    body_lines = _PRIVATE_KEY_BODY_CAP + 5
    body = [f"+{('A' * 70)}" for _ in range(body_lines)]
    diff = (
        "diff --git a/keys b/keys\n"
        "--- /dev/null\n"
        "+++ b/keys\n"
        "@@ -0,0 +1,47 @@\n"
        "+" + "-----BEGIN " + "PRIVATE KEY" + "-----" + "\n"
        + "\n".join(body) + "\n"
        "+trailing_line_beyond_cap = 1\n"
    )
    hits = scan_diff_for_secrets(diff)
    redacted = redact_secrets(diff, hits)
    # Cap body lines from the header; the line just past it survives.
    assert "trailing_line_beyond_cap = 1" in redacted
    assert redacted.count("[REDACTED:private key body]") == _PRIVATE_KEY_BODY_CAP


def test_private_key_body_cap_fits_a_real_8192_bit_key() -> None:
    """ssh-keygen -b 8192 produces ~91 body lines; the cap must not clip them."""
    assert _PRIVATE_KEY_BODY_CAP > 91


def test_push_scan_does_not_flag_bare_base64_lines() -> None:
    """The review path flags known patterns on added lines only — deliberately.

    Historical excerpts additionally redact headerless key material, because
    excerpts are stored and re-read. On the push path that shape rule would
    fire on every base64 fixture in a test suite, and that noise is what
    teaches people to ignore the warning. This pins the asymmetry so it stays
    a decision rather than drifting.
    """
    diff = (
        "diff --git a/tests/fixtures.py b/tests/fixtures.py\n"
        "--- a/tests/fixtures.py\n"
        "+++ b/tests/fixtures.py\n"
        "@@ -1,1 +1,2 @@\n"
        "+BLOB = 'U0VDUkVUTUFURVJJQUxMSU5FMjBaWlpaWlpaWlpaWlpaWlpaWlpa'\n"
    )
    assert scan_diff_for_secrets(diff) == []


def test_excerpt_key_material_rule_stays_inside_key_like_files() -> None:
    """Shape alone must not start redacting ordinary code.

    A 40-character CamelCase identifier is indistinguishable from base64. If
    the headerless-key rule acted on shape everywhere, it would destroy the
    evidence this feature exists to collect — so it applies only inside a file
    section that plausibly holds a key.
    """
    blob = "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789+/aBcD"

    def section(path: str, body: str) -> str:
        return (
            f"diff --git a/{path} b/{path}\n"
            f"--- a/{path}\n"
            f"+++ b/{path}\n"
            "@@ -1,3 +1,3 @@\n" + body
        )

    ordinary = section("src/config.py", f"+{blob}\n")
    assert blob in scrub_diff_text(ordinary), "ordinary code must not be redacted"

    key_file = section("deploy/server.pem", f"+{blob}\n")
    assert blob not in scrub_diff_text(key_file)

    # No key-ish path, but a run of consecutive base64 lines is a key body.
    run = "".join(f"-{blob}\n" for _ in range(3))
    assert blob not in scrub_diff_text(section("assets/blob.txt", run))


def test_hunk_context_suffix_is_scrubbed_in_a_key_file() -> None:
    """git copies a neighbouring line into ``@@ … @@ <context>`` verbatim."""
    blob = "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789+/aBcD"
    diff = (
        "diff --git a/deploy/k.pem b/deploy/k.pem\n"
        "--- a/deploy/k.pem\n"
        "+++ b/deploy/k.pem\n"
        f"@@ -20,3 +20,3 @@ {blob}\n"
        f"-{blob}\n"
        f"+{blob}\n"
    )
    scrubbed = scrub_diff_text(diff)
    assert scrubbed.count(blob) == 0, scrubbed
    assert "[REDACTED:private key body]" in scrubbed


def test_hunk_context_suffix_is_scrubbed_in_any_file() -> None:
    """A secret git copies into ``@@ … @@ <line>`` leaks even in a plain file."""
    diff = (
        "diff --git a/src/config.py b/src/config.py\n"
        "--- a/src/config.py\n"
        "+++ b/src/config.py\n"
        f'@@ -9,3 +9,3 @@ AWS_KEY = "{AWS_FIXTURE_KEY}"\n'
        "-line10 = 10\n"
        "+FLAG = 2\n"
    )
    for redacted in (scrub_diff_text(diff), redact_secrets(diff, scan_diff_for_secrets(diff))):
        assert AWS_FIXTURE_KEY not in redacted
        assert "[REDACTED:AWS access key]" in redacted
        # The hunk geometry must survive: it is how a reviewer finds the change.
        assert "@@ -9,3 +9,3 @@" in redacted


def test_base64_wrapped_pem_key_is_redacted() -> None:
    """A TLS key in a k8s Secret is base64 of a PEM — no ``PRIVATE KEY`` marker."""
    encoded = "LS0tLS1CRUdJTiBSU0EgUFJJVkFURSBLRVktLS0tLQ" + "pNSUlFb3dJQkFBS0NBUUVBQWIxQ2QyQW" * 3
    diff = (
        "diff --git a/secrets/prod.yaml b/secrets/prod.yaml\n"
        "--- a/secrets/prod.yaml\n"
        "+++ b/secrets/prod.yaml\n"
        "@@ -3,2 +3,2 @@ kind: Secret\n"
        "-  tls.key: OLD\n"
        f"+  tls.key: {encoded}\n"
    )
    for redacted in (scrub_diff_text(diff), redact_secrets(diff, scan_diff_for_secrets(diff))):
        assert encoded[:40] not in redacted
        assert "[REDACTED:private key body]" in redacted


def test_base64_wrapped_pem_is_redacted_outside_a_key_like_path() -> None:
    """The encoded header is distinctive, so no path gating is needed."""
    encoded = "LS0tLS1CRUdJTiBSU0EgUFJJVkFURSBLRVktLS0tLQ" + "pNSUlFb3dJQkFBS0NBUUVBQWIxQ2QyQW" * 3
    diff = (
        "diff --git a/deploy/prod.yaml b/deploy/prod.yaml\n"
        "--- a/deploy/prod.yaml\n"
        "+++ b/deploy/prod.yaml\n"
        "@@ -1,1 +1,2 @@\n"
        f"+  signing_key: {encoded}\n"
    )
    assert encoded[:40] not in scrub_diff_text(diff)


def test_ordinary_base64_that_is_not_a_pem_survives() -> None:
    """Lockfile hashes and image fixtures must not be mistaken for keys."""
    digest = "AAAAB3NzaC1yc2EAAAADAQABAAABAQC7" * 4  # base64, but not a PEM prefix
    diff = (
        "diff --git a/package-lock.json b/package-lock.json\n"
        "--- a/package-lock.json\n"
        "+++ b/package-lock.json\n"
        "@@ -1,1 +1,2 @@\n"
        f'+  "integrity": "sha512-{digest}",\n'
    )
    assert digest in scrub_diff_text(diff)


def test_key_section_shaped_base64_blobs_are_over_redacted_by_design() -> None:
    """A blob-heavy section redacts its changed lines — the documented trade."""
    blob = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8" * 2
    diff = (
        "diff --git a/fixtures/batch.png.b64 b/fixtures/batch.png.b64\n"
        "--- a/fixtures/batch.png.b64\n"
        "+++ b/fixtures/batch.png.b64\n"
        "@@ -1,3 +1,4 @@\n"
        f" {blob}\n"
        f" {blob}\n"
        f" {blob}\n"
        f"+{blob}\n"
    )
    redacted = scrub_diff_text(diff)
    assert f"+{blob}" not in redacted
    assert "+[REDACTED:private key body]" in redacted
    # Unchanged context is evidence too: only the changed line is touched.
    assert redacted.count(blob) == 3
