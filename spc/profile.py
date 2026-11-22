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
from spc.githistory import (
    CASCADE_MIN_FOLLOWUPS,
    GIT_EVIDENCE_SCHEMA,
    CommitRecord,
    GitEvidence,
    Relationship,
    evidence_to_block,
)
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

# Cascades: one change provoking several follow-up fixes. Tuples of this kind are
# the profile's most expensive-to-derive claim, so they get their own budget.
MAX_CASCADE_CHARS = 2000
MAX_CASCADE_FILES = 5
MAX_CASCADES = 12

# Event ordering: what the character budget buys first when relationships exceed
# it. Observed facts outrank hypotheses — a measured run ranked verbose
# fix_after_change hypotheses above observed reverts, the reverts never rendered,
# and the model wrote "reverts happened but without file-specific details", which
# was false. Blind reverts are already collapsed to one line, so this ordering
# only decides among events that carry files.
RELATIONSHIP_PRIORITY = {
    "revert": 0,
    "co_change": 1,
    "fix_without_test": 2,
    "fix_after_change": 3,
}


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
    "1. **Change cascades** — one bullet per line in the 'Change cascades' "
    "evidence block, stating the count verbatim, the original change, and which "
    "kinds of files needed follow-up. Put this section FIRST when that evidence "
    "is present: it is the claim a reader could not cheaply derive themselves.\n"
    "2. **Peer review themes** — what human reviewers repeatedly flag in this "
    "repo (from PR comment themes when present).\n"
    "3. **Recurring bug patterns** — specific failure modes tied to where they "
    "recur (e.g. \"N+1 queries in api/\"), not generic watchfulness.\n"
    "4. **Non-obvious conventions** — only things a newcomer could NOT guess "
    "from reading the code.\n"
    "5. **What to double-check** — concrete, repo-specific checks (e.g. \"when "
    "touching src/db/, verify migration parity\"), not generic advice.\n"
    "6. **Historical events** — one bullet per line in the 'Historical events' "
    "evidence block that carries file paths. Name the files.\n"
    "Each event line is marked [observed] (git data asserts the link) or "
    "[hypothesis] (the commits merely touch the same files within a time "
    "window). Use [observed] lines as fact and [hypothesis] lines ONLY to "
    "suggest what to watch for — never claim a hypothesis shows causation. A "
    "commit subject containing \"fix\" is a keyword, not proof of a root cause.\n"
    "NEVER collapse distinct events into a category summary. Writing \"reverts "
    "cluster in documentation and CI files\" when the evidence lists a revert "
    "of cache_utils.py + masking_utils.py and another of modeling_utils.py + "
    "fsdp.py destroys the only part a reader cannot re-derive — which files "
    "travel together. If two or more entries share the same file coupling, say "
    "that once and still name the files; otherwise give each its own bullet.\n"
    "If an event line says some reverts are \"not listed individually\" because "
    "the scanned window lacks their targets, that is a sampling limit ONLY — "
    "never write that this repo's reverts lack file details.\n"
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

    Change cascades get their own section ahead of the flat event list. They
    are the one insight here that cannot be cheaply re-derived from ``git log``
    (it takes per-commit diff overlap across the whole window), and a measured
    A/B showed that when cascades only appear as ordinary event lines the
    synthesis drops them entirely in favour of category headings.
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
    cascades = _render_cascade_section(evidence)
    if cascades:
        parts.extend(
            [
                "## Change cascades",
                "Each line states how many follow-up fixes one earlier change "
                "provoked. Report every one of them in a '## Change cascades' "
                "section at the top of the profile, using the count verbatim.",
                cascades,
            ]
        )
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


def _split_informative(
    relationships: list[Relationship], subjects: dict[str, str]
) -> tuple[list[Relationship], list[Relationship]]:
    """Split reverts into (nameable, blind).

    A revert whose target lies outside the scanned window and which touched no
    files tells the reader nothing beyond "something was undone" — it carries
    only a SHA. There were 41 such entries in a 2000-commit window of
    transformers, and rendering them individually spent over half of the
    section budget on the least informative lines.
    """
    blind: list[Relationship] = []
    informative: list[Relationship] = []
    for relationship in relationships:
        resolvable = any(sha in subjects for sha in relationship.commits[1:])
        if relationship.kind == "revert" and not relationship.files and not resolvable:
            blind.append(relationship)
        else:
            informative.append(relationship)
    return blind, informative


def _cascade_groups(evidence: GitEvidence) -> list[tuple[str, list[Relationship]]]:
    """Group ``fix_after_change`` links by the earlier commit they follow.

    Returns ``(parent_sha, relationships)`` for parents with at least
    ``CASCADE_MIN_FOLLOWUPS`` follow-ups, most follow-ups first. One change
    attracting several fixes is a different and more actionable signal than
    several unrelated fixes: it names a change *class* that historically
    breaks in more than one place.
    """
    by_parent: dict[str, list[Relationship]] = {}
    for relationship in evidence.relationships:
        if relationship.kind != "fix_after_change" or len(relationship.commits) < 2:
            continue
        by_parent.setdefault(relationship.commits[1], []).append(relationship)
    groups = [
        (parent, items)
        for parent, items in by_parent.items()
        if len(items) >= CASCADE_MIN_FOLLOWUPS
    ]
    groups.sort(key=lambda pair: (-len(pair[1]), pair[0]))
    return groups[:MAX_CASCADES]


def _render_cascade_section(evidence: GitEvidence) -> str:
    """One line per change that provoked several follow-up fixes."""
    subjects = {commit.sha: commit.subject for commit in evidence.commits}
    lines: list[str] = []
    for parent, followups in _cascade_groups(evidence):
        files = sorted({path for item in followups for path in item.files})
        more = ""
        if len(files) > MAX_CASCADE_FILES:
            more = f" (+{len(files) - MAX_CASCADE_FILES} more)"
        shown = ", ".join(files[:MAX_CASCADE_FILES]) or "no file data"
        lines.append(
            f'- {len(followups)} follow-up fixes after '
            f'"{_shorten(subjects.get(parent, parent[:10]), 75)}" — {shown}{more}'
        )
    return _cap_lines(lines, MAX_CASCADE_CHARS)


def _render_relationship_section(evidence: GitEvidence) -> str:
    """Compact, one-line-per-relationship listing — metadata only, no code.

    Each line carries the epistemic marker: ``observed`` relationships are
    stated by git data, ``hypothesis`` ones only co-locate commits in time and
    files, so the model must not phrase them as causes.

    Blind reverts collapse to one line, cascades are emitted by
    :func:`_render_cascade_section`, duplicate co-change pairs are folded, and
    what remains is ordered by information value rather than by detector order
    so the character budget buys the most explanatory events first.
    """
    if not evidence.relationships:
        return ""
    subjects = {commit.sha: commit.subject for commit in evidence.commits}
    blind, informative = _split_informative(evidence.relationships, subjects)
    cascaded = {parent for parent, _ in _cascade_groups(evidence)}

    lines: list[str] = []
    if blind:
        lines.append(
            f"- [observed] revert: {len(blind)} further reverts have no subject or "
            "files in the scanned window and are not listed individually — this "
            "is a limit of the window, NOT evidence that reverts lack file data"
        )
    seen_pairs: set[tuple[str, ...]] = set()
    ranked = sorted(
        informative,
        key=lambda item: RELATIONSHIP_PRIORITY.get(item.kind, len(RELATIONSHIP_PRIORITY)),
    )
    for relationship in ranked:
        if relationship.kind == "co_change":
            pair = tuple(sorted(relationship.files))
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
        if (
            relationship.kind == "fix_after_change"
            and len(relationship.commits) > 1
            and relationship.commits[1] in cascaded
        ):
            continue
        lines.append(_render_relationship_line(relationship, subjects))
    return _cap_lines(lines, MAX_EVENTS_CHARS)


def _render_relationship_line(relationship: Relationship, subjects: dict[str, str]) -> str:
    """One relationship as a single line, shaped per kind.

    Lines are composed from the structured fields rather than from the stored
    ``note``: the note repeats the subject and file list that are already here,
    so rendering both truncated every subject twice and made a hypothesis line
    longer than an observed one.

    ``co_change`` names the file pair first — the pair *is* the finding, and an
    earlier version that emitted only the commit count discarded the payload.
    """
    marker = "observed" if relationship.observed else "hypothesis"
    if relationship.kind == "co_change":
        return f"- [{marker}] co_change: {' + '.join(relationship.files[:2])} — {relationship.note}"
    files = ", ".join(relationship.files[:3])
    if relationship.kind == "revert":
        target = relationship.note.removeprefix("revert of ")
        return f"- [{marker}] revert: {target} — {files}" if files else (
            f"- [{marker}] revert: {target}"
        )
    fix_subject = _shorten(subjects.get(relationship.commits[0], ""), 60)
    if relationship.kind == "fix_after_change" and len(relationship.commits) > 1:
        parent = _shorten(subjects.get(relationship.commits[1], relationship.commits[1][:8]), 60)
        return f'- [{marker}] fix follows "{parent}" — {fix_subject} ({files})' if files else (
            f'- [{marker}] fix follows "{parent}" — {fix_subject}'
        )
    return f"- [{marker}] {relationship.kind}: {fix_subject} ({files})" if files else (
        f"- [{marker}] {relationship.kind}: {fix_subject}"
    )


def _render_excerpt_section(evidence: GitEvidence) -> str:
    """Bounded, redacted snippets for commits that already carry an excerpt.

    Excerpts are selected by explanatory value, not by citation order: cascade
    parents and the fixes they provoked come first. Citation order is
    detector order, which means every revert is cited before any fix — measured
    on a 2000-commit window, all 60 excerpt slots went to the revert cohort and
    none to the cascades.

    One block may claim at most half the section's budget, so an oversized
    excerpt cannot starve every later one, and the loop stops cleanly when the
    budget runs out rather than on the first block that does not fit.
    """
    with_excerpt = [commit for commit in evidence.commits if commit.excerpt]
    if not with_excerpt:
        return ""
    ordered = _order_excerpts_by_value(with_excerpt, evidence)
    blocks: list[str] = []
    used = 0
    for commit in ordered:
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


def _order_excerpts_by_value(
    with_excerpt: list[CommitRecord], evidence: GitEvidence
) -> list[CommitRecord]:
    """Cascade parents first, then their follow-ups, then everything else.

    Ordering is stable within each tier so a rerun produces the same profile.
    """
    tier: dict[str, int] = {}
    for parent, followups in _cascade_groups(evidence):
        tier.setdefault(parent, 0)
        for relationship in followups:
            tier.setdefault(relationship.commits[0], 1)
    return sorted(
        with_excerpt,
        key=lambda commit: (tier.get(commit.sha, 2), -len(commit.files)),
    )


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
