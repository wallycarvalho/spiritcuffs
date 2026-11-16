"""Load and write ``.spc.toml`` with typed defaults."""

from __future__ import annotations

import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import TypeVar

CONFIG_FILENAME = ".spc.toml"

T = TypeVar("T")


@dataclass(frozen=True)
class LlmConfig:
    """OpenAI-compatible endpoint settings."""

    base_url: str = "https://openrouter.ai/api/v1"
    model: str = "anthropic/claude-3.5-haiku"
    api_key_env: str = "SPC_API_KEY"
    timeout: float = 30.0
    # Repo-level API key (``llm.api_key`` in .spc.toml). WARNING: .spc.toml is
    # committed — the wizard never writes the key here; this field exists so a
    # user MAY choose it consciously. Usually left empty (user config / env).
    api_key: str = ""


@dataclass(frozen=True)
class ReviewConfig:
    """Review-behavior toggles."""

    show_nits: bool = False
    strict: bool = False
    max_diff_kb: int = 200
    cache_diff_hash: bool = True


@dataclass(frozen=True)
class SecretsConfig:
    """Local secrets-scan behavior."""

    block_on_secrets: bool = False


@dataclass(frozen=True)
class LearnConfig:
    """Learning-phase depth and staleness."""

    learn_max_commits: int = 2000
    profile_max_age_days: int = 30


@dataclass(frozen=True)
class GithubConfig:
    """Source B knobs: GitHub PR/comment mining during ``spc learn``."""

    enabled: str = "auto"
    base_url: str = "https://api.github.com"
    github_max_prs: int = 200
    github_max_comments: int = 1000
    github_pages_max: int = 10


@dataclass(frozen=True)
class SpcConfig:
    """The whole ``.spc.toml`` surface."""

    llm: LlmConfig = field(default_factory=LlmConfig)
    review: ReviewConfig = field(default_factory=ReviewConfig)
    secrets: SecretsConfig = field(default_factory=SecretsConfig)
    learn: LearnConfig = field(default_factory=LearnConfig)
    github: GithubConfig = field(default_factory=GithubConfig)


def config_path(repo_root: Path) -> Path:
    """Return the ``.spc.toml`` path inside *repo_root*."""
    return repo_root / CONFIG_FILENAME


def _validated(section_name: str, defaults: object, raw: object) -> dict[str, object]:
    """Filter and type-check *raw* section keys against the dataclass *defaults*.

    Unknown keys are dropped (warned once via stderr); wrong-typed values fall
    back to their default. Never raises — a broken section degrades to defaults.
    """
    defaults_dict: dict[str, object] = dict(vars(defaults))  # type: ignore[arg-type]
    if not isinstance(raw, dict):
        return dict(defaults_dict)
    cleaned: dict[str, object] = {}
    for key, value in raw.items():
        if key not in defaults_dict:
            print(
                f"spiritcuffs: ignoring unknown key {section_name}.{key!r} in {CONFIG_FILENAME}",
                file=sys.stderr,
            )
            continue
        if key == "enabled":
            normalized = _normalize_enabled(value)
            if normalized is not None:
                cleaned[key] = normalized
                continue
            print(
                f"spiritcuffs: ignoring bad value for {section_name}.{key!r} "
                "(expected \"auto\" | true | false) — using default",
                file=sys.stderr,
            )
            continue
        if not _type_matches(value, defaults_dict[key]):
            print(
                f"spiritcuffs: ignoring bad value for {section_name}.{key!r} "
                f"(expected {type(defaults_dict[key]).__name__}) — using default",
                file=sys.stderr,
            )
            continue
        cleaned[key] = value
    return {**defaults_dict, **cleaned}


def _normalize_enabled(value: object) -> str | None:
    """Normalize ``github.enabled`` spellings to "auto" | "true" | "false".

    TOML booleans are accepted (``enabled = false`` is the intuitive opt-out
    spelling and must NOT fall back to auto-mining); anything unrecognizable
    returns None so the caller warns and keeps the default.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("auto", "true", "false", "yes", "no", "1", "0"):
            return lowered
    return None


def _type_matches(value: object, default: object) -> bool:
    """True when *value* fits the field type implied by *default*.

    bool is an ``int`` subclass in Python, so it must be excluded explicitly
    from int/float fields (``max_diff_kb = true`` must never parse as 1).
    """
    if isinstance(default, bool):
        return isinstance(value, bool)
    if isinstance(value, bool):
        return False
    if isinstance(default, int):
        return isinstance(value, int)
    if isinstance(default, float):
        return isinstance(value, int | float)
    if isinstance(default, str):
        return isinstance(value, str)
    return type(value) is type(default)


def _section(section_cls: type[T], raw: object) -> T:
    """Build a config dataclass from a validated raw section (never raises)."""
    return section_cls(**_validated(section_cls.__name__, section_cls(), raw))  # type: ignore[return-value]


def load_config(repo_root: Path) -> SpcConfig:
    """Load ``.spc.toml`` from *repo_root*, falling back to defaults for missing keys.

    Precedence: repo ``.spc.toml`` > user ``~/.spc/config.toml`` > built-in
    defaults. The user config contributes only LLM endpoint defaults and
    secrets (repo values always win). Never raises: unknown keys are dropped,
    wrong-typed values fall back to defaults (each with a one-line warning) —
    a config typo must never break the push path.
    """
    path = config_path(repo_root)
    if not path.is_file():
        return SpcConfig(llm=_apply_user_llm_defaults(LlmConfig()))
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        print(
            f"spiritcuffs: could not parse {CONFIG_FILENAME} ({exc}) — using defaults",
            file=sys.stderr,
        )
        return SpcConfig(llm=_apply_user_llm_defaults(LlmConfig()))
    llm = _section(LlmConfig, raw.get("llm", {}))
    llm = _apply_user_llm_defaults(llm)
    return SpcConfig(
        llm=llm,
        review=_section(ReviewConfig, raw.get("review", {})),
        secrets=_section(SecretsConfig, raw.get("secrets", {})),
        learn=_section(LearnConfig, raw.get("learn", {})),
        github=_section(GithubConfig, raw.get("github", {})),
    )


def _apply_user_llm_defaults(llm: LlmConfig) -> LlmConfig:
    """Fill LLM fields the repo config left at built-in defaults from the user config.

    Repo values that differ from built-in defaults always win. Values equal to
    the default are treated as unset — ``spc init`` writes every key (with
    defaults), so default-equal values must not mask the user's setup choices.
    (Secret keys live only in the user config; they are resolved at call time
    via :func:`spc.userconfig.resolve_api_key`, not merged here.)
    """
    from spc.userconfig import load_user_config

    user = load_user_config()
    return LlmConfig(
        base_url=(
            llm.base_url
            if llm.base_url != LlmConfig.base_url and llm.base_url
            else (user.llm_base_url or llm.base_url)
        ),
        model=(
            llm.model
            if llm.model != LlmConfig.model and llm.model
            else (user.llm_model or llm.model)
        ),
        api_key_env=(
            llm.api_key_env
            if llm.api_key_env != LlmConfig.api_key_env and llm.api_key_env
            else (user.llm_api_key_env or llm.api_key_env)
        ),
        timeout=llm.timeout,
        api_key=llm.api_key,
    )


def render_default_config() -> str:
    """Render the default config as TOML text, with one-line comments per key."""
    return f'''[llm]
base_url = "https://openrouter.ai/api/v1"   # any OpenAI-compatible endpoint
model = "anthropic/claude-3.5-haiku"        # fast/cheap default; your choice
api_key_env = "SPC_API_KEY"                 # env var holding the key
timeout = {LlmConfig.timeout!r}                         # seconds; caps each LLM attempt

[review]
show_nits = false          # nits hidden by default (signal, not noise)
strict = false             # true => exit 1 on a critical finding (blocks push)
max_diff_kb = 200          # diff truncation ceiling; dropped files listed
cache_diff_hash = true     # reprint cached findings on identical re-review

[secrets]
block_on_secrets = false   # true => skip the LLM call when a secret is suspected

[learn]
learn_max_commits = 2000   # git history depth to mine
profile_max_age_days = 30  # staleness notice threshold (never auto-refresh)

[github]
enabled = "auto"           # auto | true | false — mine GitHub PR history in `spc learn`
base_url = "https://api.github.com"  # change for GitHub Enterprise
github_max_prs = 200       # most recent merged PRs to mine
github_max_comments = 1000 # total inline review comments fetched
github_pages_max = 10      # API pagination cap
'''


def write_default_config(repo_root: Path, *, overwrite: bool = False) -> Path | None:
    """Write ``.spc.toml`` into *repo_root*. Returns the path, or None if it already existed."""
    path = config_path(repo_root)
    if path.exists() and not overwrite:
        return None
    path.write_text(render_default_config(), encoding="utf-8")
    return path
