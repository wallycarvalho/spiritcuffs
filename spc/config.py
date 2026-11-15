"""Load and write ``.spc.toml`` with typed defaults."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_FILENAME = ".spc.toml"


@dataclass(frozen=True)
class LlmConfig:
    """OpenAI-compatible endpoint settings."""

    base_url: str = "https://openrouter.ai/api/v1"
    model: str = "anthropic/claude-3.5-haiku"
    api_key_env: str = "SPC_API_KEY"


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
    """Source B knobs. ``enabled`` is parsed but ignored until step 2."""

    enabled: str = "auto"
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


def load_config(repo_root: Path) -> SpcConfig:
    """Load ``.spc.toml`` from *repo_root*, falling back to defaults for missing keys."""
    path = config_path(repo_root)
    if not path.is_file():
        return SpcConfig()
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    return SpcConfig(
        llm=LlmConfig(**{**LlmConfig().__dict__, **raw.get("llm", {})}),
        review=ReviewConfig(**{**ReviewConfig().__dict__, **raw.get("review", {})}),
        secrets=SecretsConfig(**{**SecretsConfig().__dict__, **raw.get("secrets", {})}),
        learn=LearnConfig(**{**LearnConfig().__dict__, **raw.get("learn", {})}),
        github=GithubConfig(**{**GithubConfig().__dict__, **raw.get("github", {})}),
    )


def render_default_config() -> str:
    """Render the default config as TOML text, with one-line comments per key."""
    return '''[llm]
base_url = "https://openrouter.ai/api/v1"   # any OpenAI-compatible endpoint
model = "anthropic/claude-3.5-haiku"        # fast/cheap default; your choice
api_key_env = "SPC_API_KEY"                 # env var holding the key

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
enabled = "auto"           # auto | true | false (reserved for step 2: GitHub PR mining)
github_max_prs = 200       # most recent merged PRs to mine (step 2)
github_max_comments = 1000 # total inline review comments fetched (step 2)
github_pages_max = 10      # API pagination cap (step 2)
'''


def write_default_config(repo_root: Path, *, overwrite: bool = False) -> Path | None:
    """Write ``.spc.toml`` into *repo_root*. Returns the path, or None if it already existed."""
    path = config_path(repo_root)
    if path.exists() and not overwrite:
        return None
    path.write_text(render_default_config(), encoding="utf-8")
    return path
