"""Secrets regex scan tests."""

from __future__ import annotations

from spc.secrets import redact_secrets, scan_diff_for_secrets

DIFF_WITH_SECRETS = """diff --git a/config.py b/config.py
index 111..222 100644
--- a/config.py
+++ b/config.py
@@ -1,3 +1,5 @@
 import os
+AWS_KEY = "AKIAIOSFODNN7EXAMPLE"
+token = "ghp_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
+password = "super-secret-password-123"
 def main():
"""


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
        "-AWS_KEY = 'AKIAIOSFODNN7EXAMPLE'\n"  # removed line — not a new leak
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


def test_detects_private_key_and_jwt() -> None:
    jwt = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9P" "l_5f6K2Xm7s"
    diff = (
        "diff --git a/keys b/keys\n"
        "--- a/keys\n"
        "+++ b/keys\n"
        "@@ -0,0 +1,2 @@\n"
        "+-----BEGIN RSA PRIVATE KEY-----\n"
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
        "+-----BEGIN RSA PRIVATE KEY-----\n"
        "+MIIEpAIBAAKCAQEA7dE8wK8xExample0123456789abcdefgh\n"
        "+hqLvG2xJ9pQ0R3tU5vW7yZ9aB1cD3eF5gH7iJ9kL1mN3oP5q\n"
        "+rS7tU9vW1xY3zA5bC7dE9fG1hI3jK5lM7nO9pQ1rS3tU5v\n"
        "+wX7yZ9aB1cD3eF5gH7iJ9kL1mN3oP5qR7tU9vW1xY3zA5b\n"
        "+C7dE9fG1hI3jK5lM7nO9pQ1rS3tU5vW7yZ9aB1cD3eF5gH\n"
        "+-----END RSA PRIVATE KEY-----\n"
    )
    hits = scan_diff_for_secrets(diff)
    assert [hit.label for hit in hits] == ["private key block"]
    redacted = redact_secrets(diff, hits)
    assert "MIIEpAIBAAKCAQEA" not in redacted
    assert "hqLvG2xJ9pQ0R3tU5vW7yZ9aB1cD3eF5gH" not in redacted
    assert "[REDACTED:private key block]" in redacted
    assert redacted.count("[REDACTED:private key body]") == 5
    assert "-----END RSA PRIVATE KEY-----" in redacted  # END marker survives


def test_private_key_body_cap_bounds_redaction() -> None:
    """A missing END marker must not swallow the rest of the diff."""
    body = [f"+{('A' * 70)}" for _ in range(45)]
    diff = (
        "diff --git a/keys b/keys\n"
        "--- /dev/null\n"
        "+++ b/keys\n"
        "@@ -0,0 +1,47 @@\n"
        "+-----BEGIN PRIVATE KEY-----\n"
        + "\n".join(body) + "\n"
        "+trailing_line_beyond_cap = 1\n"
    )
    hits = scan_diff_for_secrets(diff)
    redacted = redact_secrets(diff, hits)
    # Cap = 40 body lines from the header; line 46 of the diff survives.
    assert "trailing_line_beyond_cap = 1" in redacted
    assert redacted.count("[REDACTED:private key body]") == 40
