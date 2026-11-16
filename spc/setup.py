"""Interactive onboarding wizard (``spc setup``) + non-interactive flags.

Writes the user-level config ``~/.spc/config.toml`` (mode 0600) and review
preferences into the repo ``.spc.toml`` when one exists. Idempotent: every
step offers "keep current", so re-runs edit existing choices. Fail-soft: the
wizard never crashes loudly and never writes secrets into the repo config.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel

from spc.config import LlmConfig, load_config
from spc.userconfig import (
    UserConfig,
    load_user_config,
    mask_secret,
    save_user_config,
    user_config_path,
)

WELCOME = (
    "[bold]SpiritCuffs[/] reviews your code with an LLM before every git push.\n"
    "It learns your repo's standards from git history, then reviews the pending "
    "diff — locally, in seconds."
)

KEEP_CURRENT = "keep current"


@dataclass(frozen=True)
class Provider:
    """One curated LLM provider and its sensible defaults."""

    key: str
    label: str
    base_url: str
    model: str
    api_key_env: str
    # "api_key" providers take a normal OpenAI-compatible key; "copilot"
    # providers run the GitHub device-flow login instead (spc/copilot.py).
    kind: str = "api_key"


PROVIDERS: tuple[Provider, ...] = (
    Provider(
        key="openrouter",
        label="OpenRouter",
        base_url="https://openrouter.ai/api/v1",
        model="anthropic/claude-3.5-haiku",
        api_key_env="OPENROUTER_API_KEY",
    ),
    Provider(
        key="openai",
        label="OpenAI",
        base_url="https://api.openai.com/v1",
        model="gpt-4o-mini",
        api_key_env="OPENAI_API_KEY",
    ),
    Provider(
        key="anthropic",
        label="Anthropic (OpenAI-compatible endpoint)",
        base_url="https://api.anthropic.com/v1",
        model="claude-3-5-haiku-latest",
        api_key_env="ANTHROPIC_API_KEY",
    ),
    Provider(
        key="copilot",
        label="GitHub Copilot (uses your existing Copilot subscription)",
        # Base URL is per-run from the token exchange (endpoints.api); this
        # placeholder is overridden by the copilot auth path at call time.
        base_url="https://api.githubcopilot.com",
        # Copilot plan gates model access; gpt-4.1-mini is broadly available.
        model="gpt-4.1-mini",
        api_key_env="COPILOT_GITHUB_TOKEN",
        kind="copilot",
    ),
    Provider(
        key="ollama",
        label="Local endpoint (Ollama / LM Studio / vLLM)",
        base_url="http://localhost:11434/v1",
        model="llama3.1",
        api_key_env="LLM_API_KEY",
    ),
)

# Curated shortlist per provider; free-text always wins over these.
MODEL_SHORTLISTS: dict[str, tuple[str, ...]] = {
    "openrouter": (
        "anthropic/claude-3.5-haiku",
        "openai/gpt-4o-mini",
        "google/gemini-2.0-flash-001",
        "meta-llama/llama-3.3-70b-instruct",
    ),
    "openai": ("gpt-4o-mini", "gpt-4o", "o3-mini"),
    "anthropic": ("claude-3-5-haiku-latest", "claude-3-5-sonnet-latest"),
    "copilot": ("gpt-4.1-mini", "gpt-4.1", "o4-mini"),
    "ollama": ("llama3.1", "qwen2.5-coder", "mistral"),
}


def _choose_index(choices: list[str], label: str, console: Console) -> int | None:
    """Numbered menu; returns the 0-based choice, or None to keep current / abort.

    Non-numeric free text is treated as "keep current" by callers that show a
    free-text prompt separately; menus here only need indexes.
    """
    console.print(f"\n[bold]{label}[/]")
    for index, choice in enumerate(choices, start=1):
        console.print(f"  {index}. {choice}")
    raw = typer.prompt("choice (number, empty = keep current)", default="", show_default=False)
    stripped = raw.strip()
    if not stripped:
        return None
    if stripped.isdigit() and 1 <= int(stripped) <= len(choices):
        return int(stripped) - 1
    console.print(f"[yellow]'{stripped}' is not a listed option — keeping current.[/]")
    return None


def run_setup(
    console: Console,
    *,
    non_interactive: bool = False,
    provider_key: str | None = None,
    api_key: str | None = None,
    api_key_env: str | None = None,
    model: str | None = None,
    base_url: str | None = None,
    github_token: str | None = None,
    github_token_env: str | None = None,
    copilot_token: str | None = None,
    strict: bool | None = None,
    show_nits: bool | None = None,
    skip_ping: bool = False,
) -> int:
    """Run the onboarding flow. Returns the process exit code (always 0 — fail-soft).

    Non-interactive mode skips prompts: missing values fall back to whatever is
    already stored (user config) or built-in defaults, so a CI run passes only
    the flags it cares about.
    """
    try:
        return _run_setup_inner(
            console,
            non_interactive=non_interactive,
            provider_key=provider_key,
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
    except (KeyboardInterrupt, EOFError, typer.Abort):
        console.print("\n[yellow]spiritcuffs: setup cancelled — nothing was broken.[/]")
        return 0
    except Exception as exc:  # noqa: BLE001 — the wizard must never crash loudly
        console.print(f"[yellow]spiritcuffs: setup hit an error ({exc}) — exiting.[/]")
        return 0


def _run_setup_inner(  # noqa: PLR0913 — a wizard genuinely has this many knobs
    console: Console,
    *,
    non_interactive: bool,
    provider_key: str | None,
    api_key: str | None,
    api_key_env: str | None,
    model: str | None,
    base_url: str | None,
    github_token: str | None,
    github_token_env: str | None,
    copilot_token: str | None,
    strict: bool | None,
    show_nits: bool | None,
    skip_ping: bool,
) -> int:
    console.print(Panel(WELCOME, title="spc setup", expand=False))
    user = load_user_config()
    provider = _resolve_provider(
        console, non_interactive, provider_key, base_url, api_key_env, user
    )

    copilot_just_used = False
    if provider.kind == "copilot":
        copilot_just_used = _step_copilot(
            console, non_interactive, provider, copilot_token=copilot_token
        )
    else:
        _step_api_key(
            console, non_interactive, provider, user, api_key=api_key, api_key_env=api_key_env
        )
    final_model = _step_model(console, non_interactive, provider, user, model)
    _step_github_token(
        console, non_interactive, user,
        github_token=github_token, github_token_env=github_token_env,
        copilot_just_used=copilot_just_used,
    )
    _step_preferences(console, non_interactive, strict=strict, show_nits=show_nits)
    if not skip_ping:
        _ping(console, final_model)
    _offer_repo_wiring(console, non_interactive)
    console.print("[green]setup complete.[/]")
    return 0


def _provider_by_key(
    provider_key: str, base_url: str | None, api_key_env: str | None, model: str | None
) -> Provider | None:
    """Match a --provider name; None when unknown (caller decides fallback)."""
    matched = next((p for p in PROVIDERS if p.key == provider_key), None)
    if matched is None:
        return None
    return Provider(
        key=matched.key,
        label=matched.label,
        base_url=base_url or matched.base_url,
        model=model or matched.model,
        api_key_env=api_key_env or matched.api_key_env,
        kind=matched.kind,
    )


def _resolve_provider(
    console: Console,
    non_interactive: bool,
    provider_key: str | None,
    base_url: str | None,
    api_key_env: str | None,
    user: UserConfig,
) -> Provider:
    """Pick the provider from a flag, stored config, or the interactive menu."""
    current_url = base_url or user.llm_base_url or LlmConfig.base_url
    current_env = api_key_env or user.llm_api_key_env or "SPC_API_KEY"
    fallback = Provider(
        key="custom", label="current", base_url=current_url,
        model=user.llm_model or LlmConfig.model, api_key_env=current_env,
    )
    if provider_key:
        return _provider_by_key(provider_key, base_url, api_key_env, None) or fallback
    if non_interactive:
        matched = next((p for p in PROVIDERS if p.base_url == current_url), None)
        if matched:
            return Provider(
                key=matched.key, label=matched.label, base_url=matched.base_url,
                model=user.llm_model or matched.model, api_key_env=current_env,
                kind=matched.kind,
            )
        return fallback
    choices = [f"{p.label}  [dim]({p.base_url})[/]" for p in PROVIDERS]
    choices.append(f"{KEEP_CURRENT}  [dim]({current_url})[/]")
    index = _choose_index(choices, "Which LLM provider?", console)
    if index is None or index >= len(PROVIDERS):
        return fallback
    matched = PROVIDERS[index]
    return Provider(
        key=matched.key, label=matched.label,
        base_url=base_url or matched.base_url,
        model=matched.model, api_key_env=api_key_env or matched.api_key_env,
        kind=matched.kind,
    )


def _step_api_key(
    console: Console,
    non_interactive: bool,
    provider: Provider,
    user: UserConfig,
    *,
    api_key: str | None,
    api_key_env: str | None,
) -> None:
    """Store the API key value (user config, mode 600) or just the env-var name."""
    from spc.userconfig import resolve_api_key

    if api_key is not None:
        save_user_config(
            user_config_path(),
            llm_api_key=api_key,
            llm_base_url=provider.base_url,
            llm_model=provider.model,
            llm_api_key_env=api_key_env or provider.api_key_env,
            llm_provider="generic",
        )
        console.print(
            f"[green]API key stored[/] in {user_config_path()} ({mask_secret(api_key)})"
        )
        return

    existing = resolve_api_key(
        LlmConfig(base_url=provider.base_url, api_key_env=provider.api_key_env)
    )
    if non_interactive:
        if api_key_env is not None and api_key_env.strip():
            save_user_config(
                user_config_path(),
                llm_api_key_env=api_key_env.strip(),
                llm_base_url=provider.base_url,
                llm_model=provider.model,
                llm_provider="generic",
            )
            console.print(
                f"[green]will use env var[/] [bold]{api_key_env.strip()}[/] "
                "(export it yourself; the key never touches disk)"
            )
            return
        console.print(
            f"API key: {mask_secret(existing)} [dim](set with --api-key / --api-key-env)[/]"
        )
        return

    choices = [
        f"paste an API key now [dim](stored in {user_config_path()}, mode 600)[/]",
        f"use an env var by name [dim](default: {provider.api_key_env})[/]",
        f"{KEEP_CURRENT} [dim]({mask_secret(existing)})[/]",
    ]
    index = _choose_index(choices, "API key", console)
    if index is None:
        return
    if index == 0:
        key = typer.prompt(
            "API key (hidden)", hide_input=True, default="", show_default=False
        ).strip()
        if not key:
            console.print("[yellow]empty key — skipped.[/]")
            return
        save_user_config(
            user_config_path(),
            llm_api_key=key,
            llm_base_url=provider.base_url,
            llm_model=provider.model,
            llm_api_key_env=provider.api_key_env,
            llm_provider="generic",
        )
        console.print(f"[green]key stored[/] in {user_config_path()} ({mask_secret(key)})")
    else:
        env_name = typer.prompt("env var name", default=provider.api_key_env).strip()
        if not env_name:
            console.print("[yellow]empty name — skipped.[/]")
            return
        save_user_config(user_config_path(), llm_api_key_env=env_name)
        console.print(
            f"[green]will use env var[/] [bold]{env_name}[/] "
            "(export it yourself; the key never touches disk)"
        )


def _step_copilot(
    console: Console,
    non_interactive: bool,
    provider: Provider,
    *,
    copilot_token: str | None,
) -> bool:
    """Copilot auth: device-flow login (interactive) or --copilot-token (CI).

    Only the long-lived GitHub OAuth token is stored (user config, mode 600);
    the short-lived Copilot API token from the per-run exchange is never
    persisted. GitHub Enterprise is not supported yet. Returns True when a
    Copilot token was stored THIS run (drives the mining step's "different
    token than Copilot's" clarification).
    """
    from spc import copilot
    from spc.userconfig import resolve_copilot_token

    existing = resolve_copilot_token()
    if copilot_token is not None:
        save_user_config(
            user_config_path(),
            copilot_github_token=copilot_token,
            llm_base_url=provider.base_url,
            llm_model=provider.model,
            llm_provider="copilot",
        )
        console.print(
            f"[green]Copilot GitHub token stored[/] in {user_config_path()} "
            f"({mask_secret(copilot_token)})"
        )
        return True
    if non_interactive:
        if existing:
            console.print(f"Copilot: using stored GitHub token ({mask_secret(existing)})")
            return False
        console.print(
            "[yellow]Copilot selected but no GitHub token — run `spc setup` "
            "interactively for the device-flow login, or pass "
            "--copilot-token <github-oauth-token>.[/]"
        )
        return False
    if existing and not typer.confirm(
        f"GitHub Copilot: a token is already stored ({mask_secret(existing)}). "
        "Log in again?",
        default=False,
    ):
        return False
    try:
        oauth_token = copilot.login(console.print)
    except copilot.CopilotError as exc:
        console.print(f"[yellow]Copilot login failed:[/] {exc}")
        return False
    save_user_config(
        user_config_path(),
        copilot_github_token=oauth_token,
        llm_base_url=provider.base_url,
        llm_model=provider.model,
        llm_provider="copilot",
    )
    console.print(
        f"[green]Copilot login complete[/] — token stored in {user_config_path()} "
        f"({mask_secret(oauth_token)})"
    )
    return True


def _step_model(
    console: Console,
    non_interactive: bool,
    provider: Provider,
    user: UserConfig,
    model: str | None,
) -> str:
    """Curated shortlist + keep-current; returns the final model id."""
    if model:
        save_user_config(user_config_path(), llm_model=model)
        return model
    current = user.llm_model or provider.model
    if non_interactive:
        console.print(f"model: [bold]{current}[/]")
        return current
    options = list(MODEL_SHORTLISTS.get(provider.key, ()))
    if current not in options:
        options.append(current)
    options.append(KEEP_CURRENT)
    label = f"Model [dim](current: {current})[/]"
    index = _choose_index(options, label, console)
    if index is None or options[index] == KEEP_CURRENT:
        return current
    chosen = options[index]
    save_user_config(user_config_path(), llm_model=chosen)
    return chosen


def _detect_github_token_source() -> tuple[str | None, str | None]:
    """Auto-detect a mining token WITHOUT reading the stored user-config one.

    Presentation-layer only: classifies what ``githubapi.resolve_token``
    found (env → gh probe) as ``("env", name)`` or ``("gh", None)`` so the
    wizard can say WHERE the token came from. Resolution logic itself is
    untouched; nothing is stored.
    """
    from spc.githubapi import resolve_token

    if resolve_token() is None:
        return None, None
    for env_name in ("GITHUB_TOKEN", "GH_TOKEN"):
        if os.environ.get(env_name, "").strip():
            return "env", env_name
    return "gh", None


def _guided_mining_token_choice(
    console: Console, copilot_just_used: bool
) -> None:
    """No auto-detected token: offer refresh (recommended) / paste PAT / skip."""
    console.print(
        "  1. run `gh auth refresh` [dim](recommended — uses your existing gh"
        " login, zero new storage; finish in the browser, then re-run `spc"
        " setup` or pick this again to confirm detection)[/]"
    )
    console.print(
        "  2. paste a PAT [dim](github.com/settings/tokens — fine-grained needs"
        " Contents: read + Pull requests: read; classic needs repo)[/]"
    )
    console.print("  3. skip for now [dim](mining falls back to git history only)[/]")

    def _copilot_clause() -> str:
        return (
            " [dim]Different token than Copilot's — Copilot's login only carries"
            " read:user; mining needs repo read.[/]"
            if copilot_just_used
            else ""
        )

    choice = _choose_index(
        [
            "gh auth refresh — I'll re-run/confirm after",
            "paste a personal access token",
            "skip for now",
        ],
        f"how should PR mining get a token?{_copilot_clause()}",
        console,
    )
    if choice == 1:
        token = typer.prompt(
            "GitHub token (hidden)", hide_input=True, default="", show_default=False
        ).strip()
        if not token:
            console.print("[yellow]empty token — mining stays on git history only.[/]")
            return
        save_user_config(user_config_path(), github_token=token)
        console.print(f"[green]GitHub token stored[/] ({mask_secret(token)})")
        return
    if choice == 0:
        console.print(
            "[dim]after `gh auth refresh` finishes in the browser, re-run `spc setup`"
            " — detection will pick the gh token up automatically.[/]"
        )
    # choice None or 2: skip — mining falls back to git-log-only (option text said so)


def _step_github_token(
    console: Console,
    non_interactive: bool,
    user: UserConfig,
    *,
    github_token: str | None,
    github_token_env: str | None,
    copilot_just_used: bool = False,
) -> None:
    """First-class PR-mining token step: auto-detect, else guided setup.

    PR mining makes the profile reflect how your team reviews PRs, not just
    what commits say — so the step always runs and leads with that value.
    Auto-detect order here is presentation only (gh CLI vs env); the stored
    user-config token is handled by the existing-token messaging above.
    Copilot's OAuth token (read:user) can NOT mine PRs — hence the disclaimer
    and, when needed, the guided choices.
    """
    if github_token is not None:
        save_user_config(user_config_path(), github_token=github_token)
        console.print(f"[green]GitHub token stored[/] ({mask_secret(github_token)})")
        return
    if github_token_env:
        console.print(
            "[dim]GitHub mining reads GITHUB_TOKEN / GH_TOKEN / `gh auth token` — "
            f"export {github_token_env} accordingly.[/]"
        )
        return
    if non_interactive:
        console.print(
            "[dim]PR mining: skipped (interactive step). Mining falls back to git"
            " history only — re-run `spc setup` to enable it.[/]"
        )
        return
    console.print(
        "[bold]PR mining[/] [dim]— the profile then reflects how your team"
        " reviews PRs, not just what commits say.[/]"
    )
    if user.github_token:
        console.print(
            f"[green]using your stored token[/] ({mask_secret(user.github_token)})"
        )
        return
    source, env_name = _detect_github_token_source()
    if source == "gh":
        console.print(
            "[dim]your gh CLI token works — mining will use it (no new token needed).[/]"
        )
        return
    if source == "env":
        console.print(
            f"[dim]found GitHub token in ${env_name} — mining will use it.[/]"
        )
        return
    _guided_mining_token_choice(console, copilot_just_used)


def _update_toml_section(text: str, section: str, updates: dict[str, str]) -> str:
    """Set keys inside one TOML section, preserving comments and all other bytes.

    Values arrive pre-rendered (e.g. "true", "false"). Missing keys are appended
    at the end of the section; a missing section is appended at the end of the file.
    """
    lines = text.splitlines()
    section_start = None
    section_end = len(lines)  # a section runs until the next [header] or EOF
    for index, line in enumerate(lines):
        stripped = line.strip()
        if section_start is None:
            if stripped == f"[{section}]":
                section_start = index
        elif stripped.startswith("[") and stripped.endswith("]"):
            section_end = index
            break
    if section_start is None:
        block = [f"[{section}]"] + [f"{key} = {value}" for key, value in updates.items()]
        return "\n".join(lines + block) + "\n"
    for key, value in updates.items():
        key_line = next(
            (i for i in range(section_start + 1, section_end) if lines[i].startswith(f"{key} ")),
            None,
        )
        if key_line is not None:
            trailing = _split_trailing_comment(lines[key_line])
            if trailing is None:
                lines[key_line] = f"{key} = {value}"
            else:
                left_side, comment = trailing
                # left_side is everything before the '#', e.g. `strict = false`
                key_name = left_side.split("=", 1)[0].rstrip()
                lines[key_line] = f"{key_name} = {value}  #{comment}"
        else:
            lines.insert(section_end, f"{key} = {value}")
            section_end += 1
    return "\n".join(lines) + "\n"


def _split_trailing_comment(line: str) -> tuple[str, str] | None:
    """Split ``key = value  # comment`` into (``key = value``, comment text).

    A ``#`` inside a quoted string value is NOT a comment, so quotes are
    tracked before splitting. The comment text keeps its original inner
    spacing (e.g. `` inline comment``) so the rewritten line reads as before.
    None when the line has no trailing comment.
    """
    in_string: str | None = None
    for index, char in enumerate(line):
        if in_string:
            if char == in_string:
                in_string = None
        elif char in ("'", '"'):
            in_string = char
        elif char == "#":
            return line[:index], line[index + 1 :]
    return None


def _step_preferences(
    console: Console,
    non_interactive: bool,
    *,
    strict: bool | None,
    show_nits: bool | None,
) -> None:
    """Persist review preferences into the repo .spc.toml when one exists.

    Edits only the affected key lines — a parse-and-rewrite via tomli_w would
    strip the comments that make the committed config self-documenting.
    """
    repo = Path.cwd()
    path = repo / ".spc.toml"
    if not path.is_file():
        if not non_interactive:
            console.print("[dim]no .spc.toml here — preferences will be set by `spc init`.[/]")
        return
    if strict is None and not non_interactive:
        strict = typer.confirm("strict mode (exit 1 on critical findings)?", default=False)
    if show_nits is None and not non_interactive:
        show_nits = typer.confirm("show nit-level findings?", default=False)
    updates: dict[str, str] = {}
    if strict is not None:
        updates["strict"] = str(bool(strict)).lower()
    if show_nits is not None:
        updates["show_nits"] = str(bool(show_nits)).lower()
    if not updates:
        return
    try:
        text = path.read_text(encoding="utf-8")
        tomllib.loads(text)  # refuse to touch a config we can't parse
        path.write_text(_update_toml_section(text, "review", updates), encoding="utf-8")
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError):
        console.print("[yellow]spiritcuffs: could not update .spc.toml — skipping.[/]")


def _ping(console: Console, model: str) -> None:
    """One cheap live LLM call through the real plumbing; fail-soft output.

    Single attempt (no retry ladder) so a hanging endpoint can't stall the
    wizard; auth resolution goes through the shared provider resolver
    (spc/provider.py) — the exact path reviews and learn use.
    """
    from spc import llm
    from spc.copilot import CopilotError
    from spc.diff import find_repo_root
    from spc.provider import resolve_provider_params

    repo_root = find_repo_root()
    config = load_config(repo_root) if repo_root is not None else None
    try:
        base_url, api_key, extra_headers = resolve_provider_params(config)
        text = llm.complete(
            [{"role": "user", "content": "ping — reply with the single word: pong"}],
            model=model,
            base_url=base_url,
            api_key=api_key,
            timeout=15.0,
            attempts=1,
            extra_headers=extra_headers or None,
        )
    except (llm.LlmError, CopilotError) as exc:
        console.print(f"[yellow]connection test failed:[/] {exc}")
        console.print("[dim]check base_url / model / key, then re-run `spc setup`.[/]")
        return
    reply = text.strip().splitlines()[0][:40] if text.strip() else "(empty)"
    console.print(f"[green]connection ok[/] — {model} replied: {reply!r}")


def _offer_repo_wiring(console: Console, non_interactive: bool) -> None:
    """Offer to run `spc init` when inside a git repo; print the later-command otherwise.

    Non-interactive runs never auto-init: a scripted `spc setup -y` must not
    spend LLM tokens on profile synthesis as a side effect — it prints the
    one-liner instead.
    """
    from spc.diff import find_repo_root

    if find_repo_root() is None:
        console.print("[dim]not in a git repo — later: cd <repo> && spc init[/]")
        return
    if non_interactive:
        console.print("[dim]wire up this repo later: spc init[/]")
        return
    if not typer.confirm("Wire up this repo now (spc init)?", default=True):
        return
    from spc.cli import init as init_command

    console.print("[bold]running spc init…[/]")
    init_command()
