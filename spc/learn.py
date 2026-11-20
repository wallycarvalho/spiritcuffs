"""Learning orchestrator: mine git (Source A) + GitHub (Source B), ONE synthesis call.

``run_learn`` is fail-soft end to end: git failure or empty history → warning,
False. GitHub failure → one-line notice, git-only evidence continues. LLM
failure → warning, False — NO fallback profile; user re-runs ``spc learn``.
"""

from __future__ import annotations

from pathlib import Path

from rich.console import Console

from spc.config import SpcConfig
from spc.githistory import GitEvidence, mine_git_history
from spc.githubhistory import (
    GithubEvidence,
    cluster_themes,
    decide_mining,
    detect_github_repo,
    mine_github,
)
from spc.profile import (
    build_profile,
    load_github_evidence,
    profile_path,
    save_evidence,
    save_github_evidence,
    save_profile,
)
from spc.secrets import is_change_line


def run_learn(repo_root: Path, config: SpcConfig, console: Console) -> bool:
    """Mine git history (+ GitHub PRs when available) and synthesize ``.spc/profile.md``.

    Returns True when a fresh profile was written. On git failure: one-line
    warning, returns False (exit 0 upstream). On LLM failure: one-line
    warning, returns False — NO fallback profile; user re-runs ``spc learn``.
    """
    console.print("[bold]spiritcuffs:[/] mining local git history…")
    evidence = mine_git_history(
        repo_root,
        config.learn.learn_max_commits,
        collect_excerpts=config.learn.send_code_excerpts,
    )
    if evidence is None:
        console.print(
            "[yellow]spiritcuffs:[/] no git history available — skipping learning."
        )
        return False

    save_evidence(repo_root, evidence)
    console.print(
        f"  mined {evidence.commits_scanned} commits · "
        f"{len(evidence.hotspots)} hotspots · "
        f"{len(evidence.bug_classes)} bug classes · "
        f"{len(evidence.relationships)} relationships"
    )

    extra_evidence_md = _mine_github_source(repo_root, config, console)

    console.print("[bold]spiritcuffs:[/] synthesizing profile with LLM…")
    try:
        profile_md = build_profile(evidence, config, extra_evidence_md=extra_evidence_md)
    except Exception as exc:  # noqa: BLE001 — fail soft by design; LlmError or httpx errors
        reason = str(exc).strip() or type(exc).__name__
        console.print(
            "[yellow]spiritcuffs:[/] profile synthesis failed "
            f"({reason}) — run `spc learn` again later. "
            "Continuing without a profile."
        )
        return False

    profile_md = _scrub_excerpt_leaks(profile_md, evidence, console)
    save_profile(repo_root, profile_md)
    console.print(f"[green]spiritcuffs:[/] profile written to {profile_path(repo_root)}")
    return True


def _mine_github_source(repo_root: Path, config: SpcConfig, console: Console) -> str:
    """Mine GitHub PR/comment themes and render the extra-evidence markdown.

    Never raises: any failure prints one line and returns "" (git-only learn).
    Returns "" also when mining is off, the remote is not GitHub, or nothing
    new was fetched — previously stored evidence is then re-summarized without
    network when present.
    """
    if decide_mining(config) == "off":
        console.print("[dim]spiritcuffs:[/] GitHub mining disabled (github.enabled = false).")
        return _cached_github_section(repo_root)

    previous = load_github_evidence(repo_root)
    mined = mine_github(repo_root, config, previous=previous)
    if mined is None:
        if detect_github_repo(repo_root) is None:
            console.print(
                "[dim]spiritcuffs:[/] origin is not a github remote — skipping GitHub mining."
            )
        else:
            console.print(f"[dim]spiritcuffs:[/] {_skip_reason(repo_root, config)}")
        return _cached_github_section(repo_root)

    save_github_evidence(repo_root, mined.to_dict())
    console.print(
        f"  mined {len(mined.pulls)} merged PRs · {len(mined.comments)} review comments"
    )

    themes = cluster_themes(mined, config)
    if themes is None:
        console.print(
            "[yellow]spiritcuffs:[/] theme clustering failed — "
            "continuing with git-only profile."
        )
        return ""
    filtered = _scrub_theme_section(themes, mined, console)
    return filtered.strip()


# The synthesized section is capped before it enters the profile prompt.
MAX_THEMES_CHARS = 4000
# Substrings shorter than this are too common to fingerprint a leak reliably.
MIN_LEAK_FINGERPRINT_CHARS = 20


def _scrub_theme_section(
    themes: str, evidence: GithubEvidence, console: Console
) -> str:
    """Defensively drop theme lines that leak raw-evidence text into the profile.

    PRIVACY: the LLM is instructed to synthesize, but its output is still
    checked — any line containing a raw comment snippet's long substring (or a
    raw PR title's long substring) is dropped with a one-line warning. If most
    of the section leaks, the whole section is dropped. Result is capped at
    MAX_THEMES_CHARS.
    """
    fingerprints: list[str] = []
    for comment in evidence.comments:
        snippet = " ".join(comment.body_snippet.split())
        if len(snippet) >= MIN_LEAK_FINGERPRINT_CHARS:
            fingerprints.append(snippet)
    for pull in evidence.pulls:
        title = " ".join(pull.title.split())
        if len(title) >= MIN_LEAK_FINGERPRINT_CHARS:
            fingerprints.append(title)

    kept_lines: list[str] = []
    dropped = 0
    for line in themes.splitlines():
        normalized = " ".join(line.split())
        if any(
            fingerprint in normalized or normalized in fingerprint
            for fingerprint in fingerprints
        ):
            dropped += 1
            continue
        kept_lines.append(line)
    if dropped:
        console.print(
            "[yellow]spiritcuffs:[/] dropped "
            f"{dropped} theme line(s) that quoted raw review text — "
            "privacy filter applied."
        )
    leaked_section = bool(fingerprints) and (
        len(kept_lines) < len(themes.splitlines()) / 2
    )
    if leaked_section:
        console.print(
            "[yellow]spiritcuffs:[/] theme section looked mostly like quoted "
            "review text — dropping it entirely."
        )
        return ""
    result = "\n".join(kept_lines).strip()
    if len(result) > MAX_THEMES_CHARS:
        result = result[:MAX_THEMES_CHARS] + "\n…(truncated)"
    return result


def _scrub_excerpt_leaks(
    profile_md: str, evidence: GitEvidence, console: Console
) -> str:
    """Drop profile lines that reproduce historical code.

    PRIVACY: the synthesis prompt forbids quoting excerpts, but the output is
    still checked — this profile is injected into every review prompt, so a
    quoted snippet would travel further than the one-off learn call. Any line
    containing a long run of an excerpt's code is dropped.
    """
    fingerprints = _excerpt_fingerprints(evidence)
    if not fingerprints:
        return profile_md
    kept: list[str] = []
    dropped = 0
    for line in profile_md.splitlines():
        normalized = " ".join(line.split())
        if any(fingerprint in normalized for fingerprint in fingerprints):
            dropped += 1
            continue
        kept.append(line)
    if dropped:
        console.print(
            "[yellow]spiritcuffs:[/] dropped "
            f"{dropped} profile line(s) that reproduced historical code — "
            "privacy filter applied."
        )
    return "\n".join(kept) + ("\n" if profile_md.endswith("\n") else "")


def _excerpt_fingerprints(evidence: GitEvidence) -> list[str]:
    """Long, code-shaped runs from the excerpts — too short to be a real quote otherwise.

    Every excerpt line is covered, not a sample: the filter's whole job is to
    protect the profile, so a quote from the 20th excerpt must be caught as
    surely as one from the first.
    """
    fingerprints: dict[str, None] = {}
    for commit in evidence.commits:
        for line in commit.excerpt.splitlines():
            if not is_change_line(line):
                continue
            content = " ".join(line[1:].split())
            # Skip markers and trivial one-liners: `}` or `+[REDACTED:…]`
            # would match half the profile by accident.
            if len(content) < MIN_LEAK_FINGERPRINT_CHARS or content.startswith("[REDACTED:"):
                continue
            fingerprints[content] = None
    return list(fingerprints)


def _skip_reason(repo_root: Path, config: SpcConfig) -> str:
    """One-line, accurate reason why GitHub mining was skipped this run."""
    previous = load_github_evidence(repo_root)
    if previous is not None and not previous.stats.get(
        "stopped_reason", "complete"
    ).startswith(("rate_limit", "budget", "error")):
        return "GitHub mining skipped — no new merged PRs since the last run."
    return (
        "GitHub mining skipped — no merged PRs found, or the API rejected us "
        "(check `gh auth status` / GITHUB_TOKEN, or the repo has no PRs yet)."
    )


def _cached_github_section(repo_root: Path) -> str:
    """Deterministic (no-LLM, no-network) summary of stored GitHub evidence.

    Keeps a prior mining run's value visible when re-learning offline; quietly
    returns "" when nothing is stored.
    """
    stored = load_github_evidence(repo_root)
    if stored is None or not stored.pulls:
        return ""
    lines = [
        "### GitHub review themes (from cached evidence)",
        "",
        f"Mined {len(stored.pulls)} merged PRs, {len(stored.comments)} review comments.",
    ]
    if stored.comments:
        path_counts: dict[str, int] = {}
        for comment in stored.comments:
            path = comment.path or "(general)"
            path_counts[path] = path_counts.get(path, 0) + 1
        lines.append("")
        lines.append("Most-commented paths:")
        for path, count in sorted(path_counts.items(), key=lambda kv: -kv[1])[:5]:
            lines.append(f"- `{path}` — {count} review comment(s)")
    return "\n".join(lines)


