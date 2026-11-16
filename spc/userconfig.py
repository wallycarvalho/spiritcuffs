"""User-level config ``~/.spc/config.toml``: machine-local secrets and defaults.

Holds the LLM API key and GitHub token (mode 0600) so keys never land in the
repo-committed ``.spc.toml``. Created by ``spc setup``; read by the review and
mining paths via :func:`resolve_api_key` / :func:`resolve_github_token`.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

import tomli_w

from spc.config import LlmConfig

USER_CONFIG_DIR_ENV = "SPC_USER_CONFIG"
USER_CONFIG_FILENAME = "config.toml"


@dataclass(frozen=True)
class UserConfig:
    """Secrets and machine-local defaults from ``~/.spc/config.toml``."""

    llm_api_key: str | None = None
    github_token: str | None = None
    llm_base_url: str | None = None
    llm_model: str | None = None
    llm_api_key_env: str | None = None


def user_config_path() -> Path:
    """Return the user config path, honoring ``SPC_USER_CONFIG`` for tests."""
    override = os.environ.get(USER_CONFIG_DIR_ENV)
    if override:
        return Path(override)
    return Path.home() / ".spc" / USER_CONFIG_FILENAME


def load_user_config(path: Path | None = None) -> UserConfig:
    """Parse the user config; missing file or bad TOML yields empty (never raises).

    Best-effort tightens the file to mode 0600 whenever it exists — a backup
    restore or manual creation may have left it world-readable, and a secrets
    file must never stay that way. OSError (unsupported chmod, odd ownership)
    is swallowed: reading must keep working.
    """
    path = path or user_config_path()
    if not path.is_file():
        return UserConfig()
    try:
        path.chmod(0o600)
    except OSError:
        pass
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError):
        return UserConfig()
    llm = raw.get("llm", {})
    github = raw.get("github", {})
    return UserConfig(
        llm_api_key=_nonempty_str(llm.get("api_key")),
        github_token=_nonempty_str(github.get("token")),
        llm_base_url=_nonempty_str(llm.get("base_url")),
        llm_model=_nonempty_str(llm.get("model")),
        llm_api_key_env=_nonempty_str(llm.get("api_key_env")),
    )


def _nonempty_str(value: object) -> str | None:
    """Strip *value*; None when not a non-empty string."""
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def save_user_config(
    path: Path,
    *,
    llm_api_key: str | None = None,
    github_token: str | None = None,
    llm_base_url: str | None = None,
    llm_model: str | None = None,
    llm_api_key_env: str | None = None,
) -> None:
    """Merge fields into the user config and write it with mode 0600.

    ``None`` fields are left untouched, so re-runs never wipe existing secrets.
    The mode is forced on every write (a pre-existing file may have lost it).
    """
    existing = load_user_config(path)
    data: dict[str, dict[str, str]] = {}
    llm: dict[str, str] = {}
    github: dict[str, str] = {}
    if llm_api_key is not None or existing.llm_api_key is not None:
        llm["api_key"] = llm_api_key if llm_api_key is not None else existing.llm_api_key  # type: ignore[assignment]
    if llm_base_url is not None or existing.llm_base_url is not None:
        llm["base_url"] = llm_base_url if llm_base_url is not None else existing.llm_base_url  # type: ignore[assignment]
    if llm_model is not None or existing.llm_model is not None:
        llm["model"] = llm_model if llm_model is not None else existing.llm_model  # type: ignore[assignment]
    if llm_api_key_env is not None or existing.llm_api_key_env is not None:
        llm["api_key_env"] = (
            llm_api_key_env if llm_api_key_env is not None else existing.llm_api_key_env
        )
    if github_token is not None or existing.github_token is not None:
        github["token"] = github_token if github_token is not None else existing.github_token  # type: ignore[assignment]
    if llm:
        data["llm"] = llm
    if github:
        data["github"] = github
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(tomli_w.dumps(data), encoding="utf-8")
    path.chmod(0o600)


def resolve_api_key(llm: LlmConfig | None = None) -> str | None:
    """LLM key resolution: repo ``.spc.toml`` ``api_key`` → user config → env var.

    *llm* is the merged repo-level :class:`LlmConfig`. The env var name comes
    from the repo config's ``api_key_env`` when it differs from the built-in
    default, else the user config's, else ``SPC_API_KEY``.
    """
    repo_key = (llm.api_key or "").strip() if llm is not None else ""
    if repo_key:
        return repo_key
    user = load_user_config()
    if user.llm_api_key:
        return user.llm_api_key
    env_name = "SPC_API_KEY"
    if llm is not None and llm.api_key_env and llm.api_key_env != "SPC_API_KEY":
        env_name = llm.api_key_env
    elif user.llm_api_key_env:
        env_name = user.llm_api_key_env
    return os.environ.get(env_name) or None


def resolve_github_token() -> str | None:
    """GitHub token resolution: user config ``github.token`` → env → ``gh`` probe.

    The user-config key is a machine-local secret stored by ``spc setup``;
    it wins over env vars because the user opted into persisting it.
    """
    token = load_user_config().github_token
    if token:
        return token

    # Deferred import: githubapi pulls in subprocess/shutil machinery that the
    # config layer should not need; also avoids any import-cycle risk.
    from spc.githubapi import resolve_token

    return resolve_token()


def mask_secret(value: str | None) -> str:
    """Mask a secret for display: ``sk-…abc123`` (or ``<not set>``)."""
    if not value:
        return "<not set>"
    if len(value) <= 8:
        return "…"
    return f"{value[:3]}…{value[-6:]}"
