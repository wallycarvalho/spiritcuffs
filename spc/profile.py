"""Profile: build/refresh via LLM synthesis, staleness check, load for the review prompt.

Step-1 note: ``build_profile`` accepts ``extra_evidence_md`` — reserved for the
step-2 GitHub PR/comment evidence section. Git-only runs pass nothing.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from spc import llm
from spc.config import SpcConfig
from spc.githistory import GitEvidence

ARTIFACTS_DIRNAME = ".spc"
PROFILE_FILENAME = "profile.md"
EVIDENCE_FILENAME = "evidence.json"

MAX_EVIDENCE_CHARS_FOR_PROMPT = 12000  # cap when rendering extra evidence into prompts


def artifacts_dir(repo_root: Path) -> Path:
    """``.spc/`` inside *repo_root* (gitignored working artifacts)."""
    return repo_root / ARTIFACTS_DIRNAME


def profile_path(repo_root: Path) -> Path:
    """Path of ``.spc/profile.md``."""
    return artifacts_dir(repo_root) / PROFILE_FILENAME


def evidence_path(repo_root: Path) -> Path:
    """Path of ``.spc/evidence.json``."""
    return artifacts_dir(repo_root) / EVIDENCE_FILENAME


def save_evidence(repo_root: Path, evidence: GitEvidence) -> None:
    """Persist raw git evidence to ``.spc/evidence.json`` (dataclass → JSON)."""
    directory = artifacts_dir(repo_root)
    directory.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": 1,
        "source": "git",
        "mined_at": time.time(),
        "evidence": {
            "commits_scanned": evidence.commits_scanned,
            "hotspots": evidence.hotspots,
            "bug_classes": evidence.bug_classes,
            "test_dirs": evidence.test_dirs,
            "test_co_change_ratio": evidence.test_co_change_ratio,
            "language_proportions": evidence.language_proportions,
            "total_files_tracked": evidence.total_files_tracked,
        },
    }
    evidence_path(repo_root).write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def load_evidence(repo_root: Path) -> dict | None:
    """Load previously saved evidence; None when missing/corrupt."""
    path = evidence_path(repo_root)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    evidence = payload.get("evidence")
    return evidence if isinstance(evidence, dict) else None


def build_profile(
    evidence: GitEvidence,
    config: SpcConfig,
    *,
    extra_evidence_md: str = "",
) -> str:
    """One LLM call: structured evidence → human-readable ``profile.md`` narrative.

    *extra_evidence_md* is an optional pre-rendered markdown section for
    additional evidence sources (step 2 will pass GitHub PR/comment themes
    here). Empty for git-only runs. Raises :class:`spc.llm.LlmError` on failure —
    the caller decides fail-soft behavior.
    """
    api_key = llm.resolve_api_key(config.llm.api_key_env)
    prompt = _render_synthesis_prompt(evidence, extra_evidence_md)
    return llm.complete(
        [
            {"role": "system", "content": _SYNTHESIS_SYSTEM},
            {"role": "user", "content": prompt},
        ],
        model=config.llm.model,
        base_url=config.llm.base_url,
        api_key=api_key,
        timeout=config.llm.timeout,
    )


_SYNTHESIS_SYSTEM = (
    "You are a senior engineer summarizing a repository's review standards. "
    "Write a concise, human-readable profile in markdown. Use ONLY the structured "
    "evidence given — never invent findings, never include file contents or secrets. "
    "Keep it under 400 words. Structure: a two-sentence overview, then bullet "
    "points for hotspots, historical bug clusters, test expectations, languages, "
    "and what future reviews should watch for."
)


def _render_synthesis_prompt(evidence: GitEvidence, extra_evidence_md: str) -> str:
    """Render the git-evidence JSON (+ optional extra section) for the synthesis call."""
    evidence_json = json.dumps(
        {
            "hotspots": evidence.hotspots,
            "bug_classes": evidence.bug_classes,
            "test_dirs": evidence.test_dirs,
            "test_co_change_ratio": evidence.test_co_change_ratio,
            "language_proportions": evidence.language_proportions,
            "commits_scanned": evidence.commits_scanned,
            "total_files_tracked": evidence.total_files_tracked,
        },
        indent=2,
    )
    parts = [
        "Repository evidence (JSON) follows. Synthesize the review profile now.",
        "```json",
        evidence_json,
        "```",
    ]
    if extra_evidence_md.strip():
        parts.extend(["## Additional evidence", extra_evidence_md])
    return "\n".join(parts)


def profile_age_days(repo_root: Path) -> float | None:
    """Age of ``.spc/profile.md`` in days; None when missing."""
    path = profile_path(repo_root)
    if not path.is_file():
        return None
    try:
        return max(0.0, (time.time() - path.stat().st_mtime) / 86400)
    except OSError:
        return None


def staleness_notice(repo_root: Path, max_age_days: int) -> str | None:
    """One-line notice when the profile is stale; None when fresh/absent."""
    age = profile_age_days(repo_root)
    if age is None or age <= max_age_days:
        return None
    return (
        f"spiritcuffs: profile is {age:.0f} days old "
        f"(limit {max_age_days}) — run `spc learn` to refresh. Continuing with stale profile."
    )


def load_profile(repo_root: Path) -> str | None:
    """Read the profile narrative for the review prompt; None when missing."""
    path = profile_path(repo_root)
    if not path.is_file():
        return None
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def save_profile(repo_root: Path, content: str) -> None:
    """Write the synthesized profile to ``.spc/profile.md``."""
    directory = artifacts_dir(repo_root)
    directory.mkdir(parents=True, exist_ok=True)
    profile_path(repo_root).write_text(content, encoding="utf-8")
