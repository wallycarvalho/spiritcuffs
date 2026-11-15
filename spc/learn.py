"""Learning orchestrator: mine git evidence (Source A), then ONE LLM synthesis call.

Step-2 integration point: ``run_learn`` passes ``extra_evidence_md`` into
``profile.build_profile`` — a GitHub PR/comment evidence section can be
appended there without touching this module's structure. The ``github.py``
source (Source B) plugs in beside ``mine_git_history`` below.
"""

from __future__ import annotations

from pathlib import Path

from rich.console import Console

from spc.config import SpcConfig
from spc.githistory import GitEvidence, mine_git_history
from spc.profile import (
    build_profile,
    load_evidence,
    profile_path,
    save_evidence,
    save_profile,
)


def run_learn(repo_root: Path, config: SpcConfig, console: Console) -> bool:
    """Mine git history and synthesize ``.spc/profile.md``.

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

    # Step-2 hook: render the GitHub evidence section here and pass it below.
    extra_evidence_md = ""  # step 2 fills this from .spc/evidence.json Source-B data

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


def load_cached_evidence(repo_root: Path) -> GitEvidence | None:
    """Rehydrate evidence from ``.spc/evidence.json`` (for future re-summarization)."""
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
