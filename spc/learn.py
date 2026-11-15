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
    cluster_themes,
    decide_mining,
    detect_github_repo,
    mine_github,
)
from spc.profile import (
    build_profile,
    load_evidence,
    load_github_evidence,
    profile_path,
    save_evidence,
    save_github_evidence,
    save_profile,
)


def run_learn(repo_root: Path, config: SpcConfig, console: Console) -> bool:
    """Mine git history (+ GitHub PRs when available) and synthesize ``.spc/profile.md``.

    Returns True when a fresh profile was written. On git failure: one-line
    warning, returns False (exit 0 upstream). On LLM failure: one-line
    warning, returns False — NO fallback profile; user re-runs ``spc learn``.
    """
    console.print("[bold]spiritcuffs:[/] mining local git history…")
    evidence = mine_git_history(repo_root, config.learn.learn_max_commits)
    if evidence is None:
        console.print(
            "[yellow]spiritcuffs:[/] no git history available — skipping learning."
        )
        return False

    save_evidence(repo_root, evidence)
    console.print(
        f"  mined {evidence.commits_scanned} commits · "
        f"{len(evidence.hotspots)} hotspots · "
        f"{len(evidence.bug_classes)} bug classes"
    )

    extra_evidence_md = _mine_github_source(repo_root, config, console)

    console.print("[bold]spiritcuffs:[/] synthesizing profile with LLM…")
    try:
        profile_md = build_profile(evidence, config, extra_evidence_md=extra_evidence_md)
    except Exception:  # noqa: BLE001 — fail soft by design; LlmError or httpx errors
        console.print(
            "[yellow]spiritcuffs:[/] profile synthesis failed — "
            "run `spc learn` again later. Continuing without a profile."
        )
        return False

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
            console.print(
                "[dim]spiritcuffs:[/] GitHub mining skipped "
                "(no new merged PRs, or the API was unreachable)."
            )
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
    return themes.strip()


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


def load_cached_evidence(repo_root: Path) -> GitEvidence | None:
    """Rehydrate git evidence from ``.spc/evidence.json`` (for future re-summarization)."""
    data = load_evidence(repo_root)
    if data is None:
        return None
    try:
        return GitEvidence(
            commits_scanned=int(data.get("commits_scanned", 0)),
            hotspots=[(str(p), int(n)) for p, n in data.get("hotspots", [])],
            bug_classes=list(data.get("bug_classes", [])),
            test_dirs=[str(d) for d in data.get("test_dirs", [])],
            test_co_change_ratio=float(data.get("test_co_change_ratio", 0.0)),
            language_proportions={
                str(k): float(v) for k, v in data.get("language_proportions", {}).items()
            },
            total_files_tracked=int(data.get("total_files_tracked", 0)),
        )
    except (TypeError, ValueError):
        return None
