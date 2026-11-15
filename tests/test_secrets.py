"""Secrets regex scan tests."""

from __future__ import annotations

from spc.secrets import scan_diff_for_secrets

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
