"""Profile: build/refresh via LLM synthesis, staleness check, load for the review prompt.

``build_profile`` synthesizes from two evidence sources in one call: the mined
git evidence and an optional pre-rendered markdown section carrying GitHub
PR/comment themes (``extra_evidence_md``).
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from spc import llm
from spc.config import SpcConfig
from spc.githistory import GIT_EVIDENCE_SCHEMA, GitEvidence, evidence_to_block
from spc.githubhistory import GithubEvidence

ARTIFACTS_DIRNAME = ".spc"
PROFILE_FILENAME = "profile.md"
EVIDENCE_FILENAME = "evidence.json"

# Prompt budgets. History mining now produces far more raw material than the
# synthesis call should receive: an unbounded excerpts block pushes the peer
# review section out of the model's attention, so both sections are capped here.
MAX_EVENTS_CHARS = 5000
MAX_EXCERPT_PROMPT_CHARS = 8000
# One excerpt may claim at most half the section: an oversized block must not
# crowd out every other, and truncating it keeps the section informative.
MAX_EXCERPT_BLOCK_CHARS = MAX_EXCERPT_PROMPT_CHARS // 2


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
    """Persist raw git evidence to ``.spc/evidence.json`` (dataclass → JSON).

    Schema 2 keeps per-source blocks (``git`` / ``github``) so step-2 mining
    merges alongside step-1 stats. Existing github blocks are preserved. The
    git block itself is versioned (see ``githistory.GIT_EVIDENCE_SCHEMA``).
    """
    directory = artifacts_dir(repo_root)
    directory.mkdir(parents=True, exist_ok=True)
    payload = _load_raw_payload(repo_root) or {"schema": 2, "sources": {}}
    sources = payload.setdefault("sources", {})
    sources["git"] = {
        "schema": GIT_EVIDENCE_SCHEMA,
        "source": "git",
        "mined_at": time.time(),
        "evidence": evidence_to_block(evidence),
    }
    evidence_path(repo_root).write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def save_github_evidence(repo_root: Path, github_block: dict) -> None:
    """Persist the GitHub evidence block (``githubhistory.GithubEvidence.to_dict()``).

    Merges with whatever is already stored; corrupt/foreign payloads are
    replaced rather than merged (fail-soft).
    """
    directory = artifacts_dir(repo_root)
    directory.mkdir(parents=True, exist_ok=True)
    payload = _load_raw_payload(repo_root) or {"schema": 2, "sources": {}}
    payload.setdefault("sources", {})["github"] = {
        "schema": 1,
        "source": "github",
        **github_block,
    }
    evidence_path(repo_root).write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def _load_raw_payload(repo_root: Path) -> dict | None:
    """Read evidence.json as a whole payload dict; None when missing/corrupt/foreign.

    Transparently upgrades the step-1 schema-1 shape (bare git evidence) into
    the schema-2 ``{"sources": {"git": ...}}`` wrapper.
    """
    path = evidence_path(repo_root)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("schema") == 2 and isinstance(payload.get("sources"), dict):
        return payload
    # Step-1 flat shape: {"schema":1, "source":"git", "mined_at":..., "evidence":{...}}
    evidence = payload.get("evidence")
    if isinstance(evidence, dict):
        return {
            "schema": 2,
            "sources": {"git": {**payload, "evidence": evidence}},
        }
    return None


def load_evidence(repo_root: Path) -> dict | None:
    """Load the git evidence block; None when missing/corrupt.

    Returns the flat git-evidence dict (same shape as step 1) regardless of
    whether the underlying file is schema 1 or schema 2.
    """
    payload = _load_raw_payload(repo_root)
    if payload is None:
        return None
    git_block = payload["sources"].get("git")
    if not isinstance(git_block, dict):
        return None
    evidence = git_block.get("evidence")
    return evidence if isinstance(evidence, dict) else None


def load_github_evidence(repo_root: Path) -> GithubEvidence | None:
    """Load the GitHub evidence block as a :class:`GithubEvidence`; None when absent."""
    payload = _load_raw_payload(repo_root)
    if payload is None:
        return None
    github_block = payload["sources"].get("github")
    if not isinstance(github_block, dict):
        return None
    try:
        return GithubEvidence.from_dict(github_block)
    except (TypeError, ValueError):
        return None


def build_profile(
    evidence: GitEvidence,
    config: SpcConfig,
    *,
    extra_evidence_md: str = "",
) -> str:
    """One LLM call: structured evidence → human-readable ``profile.md`` narrative.

    *extra_evidence_md* is an optional pre-rendered markdown section for
    additional evidence sources — ``spc learn`` passes the synthesized GitHub
    PR/comment themes here. Empty for git-only runs. Raises
    :class:`spc.llm.LlmError` (or :class:`spc.copilot.CopilotError`) on failure
    — the caller decides fail-soft behavior.
    """
    from spc.provider import resolve_provider_params

    prompt = _render_synthesis_prompt(evidence, extra_evidence_md)
    params = resolve_provider_params(config)
    return llm.complete(
        [
            {"role": "system", "content": _SYNTHESIS_SYSTEM},
            {"role": "user", "content": prompt},
        ],
        model=config.llm.model,
        base_url=params.base_url,
        api_key=params.api_key,
        timeout=config.llm.timeout,
        extra_headers=params.extra_headers or None,
    )


_SYNTHESIS_SYSTEM = (
    "You are a senior engineer summarizing a repository's review standards. "
    "Write a concise, human-readable profile in markdown. Use ONLY the structured "
    "evidence given — never invent findings, never include file contents or secrets. "
    "Keep it under 400 words; a short profile with only evidence-backed content "
    "beats a padded one.\n"
    "Structure (in this order, omit any section whose evidence is thin rather "
    "than padding it):\n"
    "1. **Peer review themes** — what human reviewers repeatedly flag in this "
    "repo (from PR comment themes when present). This is the headline section.\n"
    "2. **Recurring bug patterns** — specific failure modes tied to where they "
    "recur (e.g. \"N+1 queries in api/\"), not generic watchfulness.\n"
    "3. **Non-obvious conventions** — only things a newcomer could NOT guess "
    "from reading the code.\n"
    "4. **What to double-check** — concrete, repo-specific checks (e.g. \"when "
    "touching src/db/, verify migration parity\"), not generic advice.\n"
    "A \"Historical events\" section may list past fix/revert relationships. Each "
    "line is marked [observed] (git data asserts the link) or [hypothesis] (the "
    "commits merely touch the same files within a time window). Use [observed] "
    "lines as fact and [hypothesis] lines ONLY to suggest what to watch for — "
    "never claim a hypothesis shows causation. A commit subject containing "
    "\"fix\" is a keyword, not proof of a root cause. When the same failure "
    "recurs in two or more relationship entries, say so as a pattern; for a "
    "single entry, describe it as an incident, not a rule.\n"
    "Historical code excerpts may be provided to show the shape of past "
    "failures. Never quote, paraphrase, or restate that code — extract only the "
    "failure mode in plain language.\n"
    "Do NOT include statistics. Do NOT state language composition. Do NOT give "
    "advice that would be equally true of any repository (e.g. \"ensure tests "
    "accompany changes\", \"keep documentation up to date\", \"watch for "
    "regressions in complex code\")."
)


def _render_synthesis_prompt(evidence: GitEvidence, extra_evidence_md: str) -> str:
    """Render the git-evidence JSON (+ optional extra sections) for the synthesis call.

    Language proportions and test co-change stats are deliberately omitted —
    they invite the model to restate numbers (the exact generic padding the
    profile must not contain). They remain in evidence.json for the curious.

    Peer-review evidence comes before the git-history sections: it is the
    headline of the profile, and a long excerpts block placed ahead of it
    competes with it for the model's attention, pulling the synthesis away
    from what reviewers actually said.
    """
    evidence_json = json.dumps(
        {
            "hotspots": evidence.hotspots,
            "bug_classes": evidence.bug_classes,
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
        parts.extend(["## Peer review evidence", extra_evidence_md])
    events = _render_relationship_section(evidence)
    if events:
        parts.extend(["## Historical events", events])
    excerpts = _render_excerpt_section(evidence)
    if excerpts:
        parts.extend(
            [
                "## Historical code excerpts",
                "These are diffs from past fix/revert commits. They show the SHAPE "
                "of previous failures. Never quote this code in the profile.",
                excerpts,
            ]
        )
    return "\n".join(parts)


def _render_relationship_section(evidence: GitEvidence) -> str:
    """Compact, one-line-per-relationship listing — metadata only, no code.

    Each line carries the epistemic marker: ``observed`` relationships are
    stated by git data, ``hypothesis`` ones only co-locate commits in time and
    files, so the model must not phrase them as causes.
    """
    if not evidence.relationships:
        return ""
    subjects = {commit.sha: commit.subject for commit in evidence.commits}
    lines: list[str] = []
    for relationship in evidence.relationships:
        marker = "observed" if relationship.observed else "hypothesis"
        commits = ", ".join(
            f"{sha[:8]} {_shorten(subjects.get(sha, ''))}"
            for sha in relationship.commits[:3]
        )
        files = ", ".join(relationship.files[:4])
        line = f"- [{marker}] {relationship.kind}: {relationship.note}"
        detail = " — ".join(part for part in (commits, files) if part)
        if detail:
            line += f" ({detail})"
        lines.append(line)
    return _cap_lines(lines, MAX_EVENTS_CHARS)


def _render_excerpt_section(evidence: GitEvidence) -> str:
    """Bounded, redacted snippets for commits that already carry an excerpt.

    One block may claim at most half the section's budget, so an oversized
    excerpt cannot starve every later one, and the loop stops cleanly when the
    budget runs out rather than on the first block that does not fit.
    """
    with_excerpt = [commit for commit in evidence.commits if commit.excerpt]
    if not with_excerpt:
        return ""
    blocks: list[str] = []
    used = 0
    for commit in with_excerpt:
        header = f"### {commit.sha[:8]} {_shorten(commit.subject)}"
        block = f"{header}\n```diff\n{commit.excerpt}\n```"
        remaining = MAX_EXCERPT_PROMPT_CHARS - used
        if remaining < len(header) + 32:
            break
        allowance = min(remaining, MAX_EXCERPT_BLOCK_CHARS)
        if len(block) > allowance:
            block = _cap_excerpt_block(block, allowance)
        blocks.append(block)
        used += len(block)
    return "\n\n".join(blocks)


def _cap_excerpt_block(block: str, max_chars: int) -> str:
    """Truncate one fenced excerpt block, closing its fence and marking the cut."""
    if len(block) <= max_chars:
        return block
    return block[: max_chars - 16].rstrip() + "\n…(truncated)\n```"


def _cap_lines(lines: list[str], max_chars: int) -> str:
    """Join lines up to *max_chars*, noting any truncation."""
    kept: list[str] = []
    used = 0
    for line in lines:
        if used + len(line) + 1 > max_chars:
            kept.append("…(truncated)")
            break
        kept.append(line)
        used += len(line) + 1
    return "\n".join(kept)


def _shorten(text: str, limit: int = 64) -> str:
    """Collapse whitespace and trim long subjects for one-line rendering."""
    collapsed = " ".join(text.split())
    return collapsed if len(collapsed) <= limit else collapsed[: limit - 1] + "…"


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
