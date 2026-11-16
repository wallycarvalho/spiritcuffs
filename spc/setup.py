"""Interactive onboarding wizard (``spc setup``) + non-interactive flags.

Writes the user-level config ``~/.spc/config.toml`` (mode 0600) and review
preferences into the repo ``.spc.toml`` when one exists. Idempotent: every
step offers "keep current", so re-runs edit existing choices. Fail-soft: the
wizard never crashes loudly and never writes secrets into the repo config.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

import tomli_w
import typer
from rich.console import Console
from rich.panel import Panel

from spc.config import LlmConfig
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
            strict=strict,
            show_nits=show_nits,
            skip_ping=skip_ping,
        )
    except KeyboardInterrupt:
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
    strict: bool | None,
    show_nits: bool | None,
    skip_ping: bool,
) -> int:
    console.print(Panel(WELCOME, title="spc setup", expand=False))
    user = load_user_config()
    provider = _resolve_provider(
        console, non_interactive, provider_key, base_url, api_key_env, user
    )

    _step_api_key(
        console, non_interactive, provider, user, api_key=api_key, api_key_env=api_key_env
    )
    final_model = _step_model(console, non_interactive, provider, user, model)
    _step_github_token(
        console, non_interactive, user,
        github_token=github_token, github_token_env=github_token_env,
    )
    _step_preferences(console, non_interactive, strict=strict, show_nits=show_nits)
    if not skip_ping:
        _ping(console, provider.base_url, final_model)
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
        )
        console.print(
            f"[green]API key stored[/] in {user_config_path()} ({mask_secret(api_key)})"
        )
        return

    existing = resolve_api_key(
        LlmConfig(base_url=provider.base_url, api_key_env=provider.api_key_env)
    )
    if non_interactive:
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


def _step_github_token(
    console: Console,
    non_interactive: bool,
    user: UserConfig,
    *,
    github_token: str | None,
    github_token_env: str | None,
) -> None:
    """Optional GitHub PAT storage (feeds step-2 mining); fully skip-able."""
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
        return
    if not typer.confirm("Store a GitHub token for PR mining? (optional)", default=False):
        return
    token = typer.prompt(
        "GitHub token (hidden)", hide_input=True, default="", show_default=False
    ).strip()
    if not token:
        console.print("[yellow]empty token — skipped.[/]")
        return
    save_user_config(user_config_path(), github_token=token)
    console.print(f"[green]GitHub token stored[/] ({mask_secret(token)})")


def _step_preferences(
    console: Console,
    non_interactive: bool,
    *,
    strict: bool | None,
    show_nits: bool | None,
) -> None:
    """Persist review preferences into the repo .spc.toml when one exists."""
    repo = Path.cwd()
    path = repo / ".spc.toml"
    if not path.is_file():
        if not non_interactive:
            console.print("[dim]no .spc.toml here — preferences will be set by `spc init`.[/]")
        return
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError):
        console.print("[yellow]spiritcuffs: could not parse .spc.toml — skipping preferences.[/]")
        return
    review = raw.setdefault("review", {})
    if strict is None and not non_interactive:
        strict = typer.confirm("strict mode (exit 1 on critical findings)?", default=False)
    if show_nits is None and not non_interactive:
        show_nits = typer.confirm("show nit-level findings?", default=False)
    if strict is not None:
        review["strict"] = bool(strict)
    if show_nits is not None:
        review["show_nits"] = bool(show_nits)
    try:
        path.write_text(tomli_w.dumps(raw), encoding="utf-8")
    except OSError:
        console.print("[yellow]spiritcuffs: could not write .spc.toml — skipping.[/]")


def _ping(console: Console, base_url: str, model: str) -> None:
    """One cheap live LLM call through the real plumbing; fail-soft output."""
    from spc import llm
    from spc.userconfig import resolve_api_key

    api_key = resolve_api_key(LlmConfig(base_url=base_url, model=model))
    try:
        text = llm.complete(
            [{"role": "user", "content": "ping — reply with the single word: pong"}],
            model=model,
            base_url=base_url,
            api_key=api_key,
            timeout=15.0,
        )
    except llm.LlmError as exc:
        console.print(f"[yellow]connection test failed:[/] {exc}")
        console.print("[dim]check base_url / model / key, then re-run `spc setup`.[/]")
        return
    reply = text.strip().splitlines()[0][:40] if text.strip() else "(empty)"
    console.print(f"[green]connection ok[/] — {model} replied: {reply!r}")


def _offer_repo_wiring(console: Console, non_interactive: bool) -> None:
    """Offer to run `spc init` when inside a git repo; print the later-command otherwise."""
    from spc.diff import find_repo_root

    if find_repo_root() is None:
        console.print("[dim]not in a git repo — later: cd <repo> && spc init[/]")
        return
    if not non_interactive and not typer.confirm("Wire up this repo now (spc init)?", default=True):
        return
    from spc.cli import init as init_command

    console.print("[bold]running spc init…[/]")
    init_command()
