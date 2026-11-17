"""Typer CLI entrypoints: init, learn, review, hook, history."""

from __future__ import annotations

import sys
from hashlib import sha256
from pathlib import Path

import typer
from rich.console import Console

from spc import __version__
from spc import diff as diff_mod
from spc import hook as hook_mod
from spc import log as log_mod
from spc import review as review_mod
from spc.config import SpcConfig, load_config, write_default_config
from spc.conventions import discover_conventions
from spc.diff import DiffResult, collect_diff, diff_hash, find_repo_root, truncate_diff
from spc.log import cache_key
from spc.profile import load_profile, staleness_notice
from spc.secrets import redact_secrets, scan_diff_for_secrets

app = typer.Typer(
    help="SpiritCuffs — an LLM second opinion on every push.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()


def _require_repo() -> Path:
    """Resolve the repo root or exit with a friendly error."""
    root = find_repo_root()
    if root is None:
        console.print("[red]spiritcuffs:[/] not inside a git repository.")
        raise typer.Exit(0)
    return root


@app.command()
def init() -> None:
    """One-time setup: config, hook, conventions, learning, then the first review."""
    repo_root = _require_repo()
    config = load_config(repo_root)

    if write_default_config(repo_root) is None:
        console.print(f"[dim].spc.toml already exists — keeping it.[/] ({repo_root / '.spc.toml'})")
    else:
        console.print("[green]wrote .spc.toml[/] (defaults — edit to taste)")

    console.print(f"project type: [bold]{_detect_project_type(repo_root)}[/]")

    result = hook_mod.install_hook(repo_root)
    hook_word = {"installed": "installed", "already": "already installed", "replaced": "refreshed"}
    console.print(f"pre-push hook: [bold]{hook_word.get(result, result)}[/]")

    _ensure_gitignore(repo_root)

    conventions = discover_conventions(repo_root)
    found = ", ".join(item.name for item in conventions) or "none"
    console.print(f"conventions found: {found}")

    from spc.learn import run_learn

    run_learn(repo_root, config, console)

    console.print("[bold]spiritcuffs:[/] running your first review…")
    exit_code = execute_review(repo_root, config)
    raise typer.Exit(exit_code)


@app.command()
def learn() -> None:
    """Re-mine git history and re-synthesize .spc/profile.md (manual; never automatic)."""
    repo_root = _require_repo()
    config = load_config(repo_root)
    from spc.learn import run_learn

    run_learn(repo_root, config, console)


@app.command()
def review(
    from_hook: bool = typer.Option(
        False,
        "--from-hook",
        help="Read pre-push stdin refs and review exactly the commits being pushed.",
    ),
) -> None:
    """Review the pending diff — run by the pre-push hook (or manually).

    Hook mode (--from-hook): parses git's pre-push stdin and reviews the
    actual push diff. Manual mode: upstream diff, staged fallback — unchanged.
    """
    repo_root = _require_repo()
    config = load_config(repo_root)
    stdin_text = None
    if from_hook:
        stdin_text = sys.stdin.read() if not sys.stdin.isatty() else ""
    raise typer.Exit(execute_review(repo_root, config, push_stdin=stdin_text))


def execute_review(
    repo_root: Path, config: SpcConfig, push_stdin: str | None = None
) -> int:
    """The whole review flow, shared by the command and ``init``.

    *push_stdin*: pre-push hook stdin content (``--from-hook`` mode). When
    given, the diff is computed from the pushed refs instead of @{upstream}.

    Returns the process exit code: 0 unless strict + a critical finding
    (fresh OR cached) or block_on_secrets triggered. Fail-soft by design:
    any unexpected error returns 0 (never breaks a push); strict + critical
    and block_on_secrets are the ONLY non-zero paths.
    """
    try:
        return _execute_review_inner(repo_root, config, push_stdin)
    except Exception as exc:  # noqa: BLE001 — the push path must never crash
        console.print(
            f"[yellow]spiritcuffs:[/] review incomplete — unexpected error ({exc}); "
            "continuing (nothing was concluded)."
        )
        return 0


def _execute_review_inner(
    repo_root: Path, config: SpcConfig, push_stdin: str | None = None
) -> int:
    """Review pipeline proper; see :func:`execute_review` for the contract."""
    if push_stdin is not None:
        refs = diff_mod.parse_push_lines(push_stdin)
        diff_result = diff_mod.push_diff(repo_root, refs) if refs else None
    else:
        diff_result = collect_diff(repo_root)
    if diff_result is None:
        return 0  # empty / deletion-only / no-refs push: nothing to review
    truncated = False

    # Redact secrets BEFORE truncation: redaction must not be what drops files,
    # and the LLM must never see the detected secret spans.
    secret_hits = scan_diff_for_secrets(diff_result.text)
    if secret_hits:
        labels = ", ".join(sorted({hit.label for hit in secret_hits}))
        console.print(f"[bold red]⚠ possible secret(s) in diff:[/] {labels}")
        console.print("[bold red]  do NOT push before removing them.[/]")
        if config.secrets.block_on_secrets:
            console.print(
                "[yellow]spiritcuffs: block_on_secrets=true — push blocked. "
                "(bypass: git push --no-verify)[/]"
            )
            return 1
        diff_result = DiffResult(
            text=redact_secrets(diff_result.text, secret_hits),
            source=diff_result.source,
            files=diff_result.files,
            dropped_files=diff_result.dropped_files,
        )

    truncated_text, dropped = truncate_diff(diff_result.text, config.review.max_diff_kb)
    if dropped:
        diff_result = DiffResult(
            text=truncated_text, source=diff_result.source,
            files=diff_result.files, dropped_files=dropped,
        )
        console.print(
            f"[yellow]spiritcuffs: review incomplete — {len(dropped)} file(s) omitted "
            "due to size limits.[/]"
        )
        truncated = True

    notice = staleness_notice(repo_root, config.learn.profile_max_age_days)
    if notice:
        console.print(f"[yellow]{notice}[/]")

    profile = load_profile(repo_root)
    conventions = discover_conventions(repo_root)
    hotspots = _hotspot_paths(repo_root)
    profile_hash = sha256((profile or "").encode("utf-8")).hexdigest()
    conventions_hash = sha256(
        "\n".join(
            f"{item.name}\n{sha256(item.content.encode('utf-8')).hexdigest()}"
            for item in conventions
        ).encode("utf-8")
    ).hexdigest()

    digest = cache_key(
        diff_hash(diff_result.text),
        model=config.llm.model,
        strict=config.review.strict,
        show_nits=config.review.show_nits,
        profile_hash=profile_hash,
        conventions_hash=conventions_hash,
    )

    if config.review.cache_diff_hash:
        cached_record = log_mod.cache_hit_message(repo_root, digest)
        if cached_record is not None:
            console.print(
                "[dim]spiritcuffs: diff unchanged since last review — cached findings:[/]"
            )
            cached_findings = [
                review_mod.Finding(
                    severity=str(f.get("severity", "warn")),
                    file=str(f.get("file", "?")),
                    line=int(f.get("line", 0) or 0),
                    message=str(f.get("message", "")),
                    suggestion=str(f.get("suggestion", "")),
                )
                for f in cached_record.get("findings", [])
                if isinstance(f, dict)
            ]
            _print_findings(cached_findings, console)
            if config.review.strict and any(
                f.severity == "critical" for f in cached_findings
            ):
                console.print(
                    "[bold red]strict mode: critical finding — push blocked.[/] "
                    "(bypass: git push --no-verify)"
                )
                return 1
            return 0

    output = review_mod.run_review(
        diff_result, profile, conventions, config, console, hotspot_paths=hotspots
    )
    if truncated and output.ok:
        output.outcome = "incomplete"

    if not output.ok:
        # Failed review: the yellow LLM warning was already printed by
        # run_review. Never a green "no substantive findings" line, nothing
        # logged, nothing cached — the next push re-reviews for real.
        return 0

    _print_findings(output.findings, console)

    try:
        log_mod.append_record(
            repo_root,
            diff_hash=digest,
            diff=diff_result,
            findings=[
                {
                    "severity": finding.severity,
                    "file": finding.file,
                    "line": finding.line,
                    "message": finding.message,
                    "suggestion": finding.suggestion,
                }
                for finding in output.findings
            ],
            model=output.model,
            outcome=output.outcome,
        )
    except OSError as exc:
        console.print(f"[yellow]spiritcuffs:[/] could not write history: {exc}")

    if config.review.strict and any(f.severity == "critical" for f in output.findings):
        console.print(
            "[bold red]strict mode: critical finding — push blocked.[/] "
            "(bypass: git push --no-verify)"
        )
        return 1
    return 0


def _hotspot_paths(repo_root: Path) -> set[str]:
    """File paths worth extra scrutiny, from cached evidence (advisory; never fatal)."""
    from spc.profile import load_evidence

    try:
        data = load_evidence(repo_root)
        if not data:
            return set()
        hotspots = []
        for entry in data.get("hotspots", [])[:20]:
            if isinstance(entry, list) and len(entry) == 2:
                hotspots.append(str(entry[0]))
        return {p for p in hotspots if "." in Path(p).name}  # files only, not dirs
    except Exception:  # noqa: BLE001 — hotspots are advisory only
        return set()


def _print_findings(findings: list, console: Console) -> None:
    """Rich-print findings, highest severity first."""
    from spc.review import rank_findings

    for finding in rank_findings(findings):
        style = review_mod.SEVERITY_STYLES.get(finding.severity, "white")
        location = f"{finding.file}:{finding.line}" if finding.line else finding.file
        console.print(f"[{style}]● {finding.severity.upper()}[/] [bold]{location}[/]")
        console.print(f"  {finding.message}")
        if finding.suggestion:
            console.print(f"  [dim]→ {finding.suggestion}[/]")
    if not findings:
        console.print("[green]spiritcuffs: no substantive findings.[/]")


def _detect_project_type(repo_root: Path) -> str:
    """Detect the project's ecosystem from its manifest files."""
    markers = {
        "pyproject.toml": "python",
        "package.json": "node",
        "go.mod": "go",
        "Cargo.toml": "rust",
    }
    detected = [eco for manifest, eco in markers.items() if (repo_root / manifest).is_file()]
    return ", ".join(detected) if detected else "unknown"


def _ensure_gitignore(repo_root: Path) -> None:
    """Add ``.spc/`` to .gitignore (preferred) or .git/info/exclude."""
    gitignore = repo_root / ".gitignore"
    info_exclude = repo_root / ".git" / "info" / "exclude"
    target = gitignore if gitignore.is_file() else info_exclude
    try:
        existing = target.read_text(encoding="utf-8") if target.exists() else ""
        if ".spc/" not in existing:
            separator = "" if not existing or existing.endswith("\n") else "\n"
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("a", encoding="utf-8") as handle:
                handle.write(f"{separator}.spc/\n")
    except OSError:
        console.print("[yellow]spiritcuffs:[/] could not update gitignore — add `.spc/` manually.")


@app.command()
def hook(
    remove: bool = typer.Option(False, "--remove", help="Uninstall the pre-push hook."),
) -> None:
    """Install (or with --remove, uninstall) the git pre-push hook."""
    repo_root = _require_repo()
    if remove:
        removed = hook_mod.remove_hook(repo_root)
        console.print(
            "[green]hook removed.[/]" if removed else "[dim]no spiritcuffs hook found.[/]"
        )
        return
    result = hook_mod.install_hook(repo_root)
    console.print(f"pre-push hook: [bold]{result}[/]")


@app.command()
def history(
    limit: int = typer.Option(20, "--limit", "-n", help="How many runs to show."),
) -> None:
    """Pretty-print recent review runs for this repo."""
    repo_root = _require_repo()
    log_mod.print_history(repo_root, console, limit=limit)


@app.command()
def version() -> None:
    """Print the installed spiritcuffs version."""
    console.print(f"spiritcuffs {__version__}")


@app.command()
def setup(
    non_interactive: bool = typer.Option(
        False, "--non-interactive", "-y", help="No prompts; use flags + stored config."
    ),
    provider: str | None = typer.Option(
        None, "--provider", help="openrouter | openai | anthropic | copilot | ollama"
    ),
    api_key: str | None = typer.Option(
        None, "--api-key", help="API key value (stored user-level, mode 600)."
    ),
    api_key_env: str | None = typer.Option(
        None, "--api-key-env", help="Env var NAME to use instead of storing the key."
    ),
    model: str | None = typer.Option(None, "--model", help="Model id override."),
    base_url: str | None = typer.Option(None, "--base-url", help="OpenAI-compatible base URL."),
    github_token: str | None = typer.Option(
        None, "--github-token", help="GitHub PAT (stored user-level, mode 600)."
    ),
    github_token_env: str | None = typer.Option(
        None, "--github-token-env", help="GitHub token env var NAME (not stored on disk)."
    ),
    copilot_token: str | None = typer.Option(
        None,
        "--copilot-token",
        help="GitHub OAuth token for Copilot (stored user-level, mode 600).",
    ),
    strict: bool | None = typer.Option(None, "--strict/--no-strict", help="Review strict mode."),
    show_nits: bool | None = typer.Option(
        None, "--nits/--no-nits", help="Show nit-level findings."
    ),
    skip_ping: bool = typer.Option(False, "--skip-ping", help="Skip the connection test."),
) -> None:
    """Interactive onboarding: provider, key, model, preferences, connection test."""
    from spc.setup import run_setup

    raise typer.Exit(
        run_setup(
            console,
            non_interactive=non_interactive,
            provider_key=provider,
            api_key=api_key,
            api_key_env=api_key_env,
            model=model,
            base_url=base_url,
            github_token=github_token,
            github_token_env=github_token_env,
            copilot_token=copilot_token,
            strict=strict,
            show_nits=show_nits,
            skip_ping=skip_ping,
        )
    )


def main() -> None:
    """Console-script entrypoint."""
    app()


if __name__ == "__main__":
    main()
