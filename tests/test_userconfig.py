"""User-level config (~/.spc/config.toml): precedence, mode 600, masking, token chain."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from spc.config import LlmConfig
from spc.userconfig import (
    UserConfig,
    load_user_config,
    mask_secret,
    resolve_api_key,
    save_user_config,
    user_config_path,
)


@pytest.fixture()
def user_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the user config to a temp dir and clear token env vars."""
    target = tmp_path / "usercfg" / "config.toml"
    monkeypatch.setenv("SPC_USER_CONFIG", str(target))
    for var in ("SPC_API_KEY", "OPENROUTER_API_KEY", "OPENAI_API_KEY", "GITHUB_TOKEN", "GH_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    return target


def test_user_config_path_honors_env_override(user_home: Path) -> None:
    assert user_config_path() == user_home


def test_load_missing_file_yields_empty(user_home: Path) -> None:
    assert load_user_config() == UserConfig()


def test_load_garbage_toml_yields_empty(user_home: Path) -> None:
    user_home.parent.mkdir(parents=True, exist_ok=True)
    user_home.write_text("not [valid toml", encoding="utf-8")
    assert load_user_config() == UserConfig()


def test_save_creates_file_with_mode_600(user_home: Path) -> None:
    save_user_config(user_home, llm_api_key="sk-secret-value")
    assert user_home.is_file()
    mode = stat.S_IMODE(user_home.stat().st_mode)
    assert mode == 0o600


def test_save_creates_parent_dirs_with_tight_permissions(user_home: Path) -> None:
    save_user_config(user_home, llm_api_key="sk-abc")
    # The parent dir must exist; the FILE must be 0600 regardless of umask.
    assert user_home.parent.is_dir()
    assert stat.S_IMODE(user_home.stat().st_mode) == 0o600


def test_save_merges_and_never_wipes_existing_keys(user_home: Path) -> None:
    save_user_config(user_home, llm_api_key="sk-first")
    save_user_config(user_home, github_token="gho-token")
    config = load_user_config()
    assert config.llm_api_key == "sk-first"
    assert config.github_token == "gho-token"


def test_save_roundtrip_full_payload(user_home: Path) -> None:
    save_user_config(
        user_home,
        llm_api_key="sk-1",
        github_token="gh-2",
        llm_base_url="https://api.openai.com/v1",
        llm_model="gpt-4o-mini",
        llm_api_key_env="OPENAI_API_KEY",
    )
    config = load_user_config()
    assert config == UserConfig(
        llm_api_key="sk-1",
        github_token="gh-2",
        llm_base_url="https://api.openai.com/v1",
        llm_model="gpt-4o-mini",
        llm_api_key_env="OPENAI_API_KEY",
    )


def test_resolve_api_key_repo_key_wins_over_everything(
    user_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_user_config(user_home, llm_api_key="sk-user")
    monkeypatch.setenv("SPC_API_KEY", "sk-env")
    repo = LlmConfig(api_key="sk-repo")
    assert resolve_api_key(repo) == "sk-repo"


def test_resolve_api_key_user_config_beats_env(
    user_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_user_config(user_home, llm_api_key="sk-user")
    monkeypatch.setenv("SPC_API_KEY", "sk-env")
    assert resolve_api_key(LlmConfig()) == "sk-user"


def test_resolve_api_key_env_is_last_resort(
    user_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SPC_API_KEY", "sk-env")
    assert resolve_api_key(LlmConfig()) == "sk-env"


def test_resolve_api_key_honors_custom_env_var_name(
    user_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or")
    assert resolve_api_key(LlmConfig(api_key_env="OPENROUTER_API_KEY")) == "sk-or"


def test_resolve_api_key_user_config_can_supply_env_var_name(
    user_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_user_config(user_home, llm_api_key_env="OPENAI_API_KEY")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-oa")
    assert resolve_api_key(LlmConfig()) == "sk-oa"


def test_resolve_api_key_repo_env_name_beats_user_env_name(
    user_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_user_config(user_home, llm_api_key_env="OPENAI_API_KEY")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or")
    assert resolve_api_key(LlmConfig(api_key_env="OPENROUTER_API_KEY")) == "sk-or"


def test_resolve_api_key_none_anywhere(user_home: Path) -> None:
    assert resolve_api_key(LlmConfig()) is None


def test_mask_secret_full_never_printed(user_home: Path) -> None:
    value = "sk-proj-1234567890abcdef"
    masked = mask_secret(value)
    # Middle must be gone, tail recognizable, total shorter than the secret.
    assert "1234567890" not in masked
    assert "abcdef" in masked
    assert len(masked) < len(value)


def test_mask_secret_short_value(user_home: Path) -> None:
    assert mask_secret("short") == "…"


def test_mask_secret_none(user_home: Path) -> None:
    assert mask_secret(None) == "<not set>"


def test_resolve_github_token_user_config_wins(
    user_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from spc.userconfig import resolve_github_token

    save_user_config(user_home, github_token="gho-from-config")
    monkeypatch.setenv("GITHUB_TOKEN", "gho-from-env")
    assert resolve_github_token() == "gho-from-config"


def test_resolve_github_token_falls_through_to_env(
    user_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from spc.userconfig import resolve_github_token

    monkeypatch.setenv("GITHUB_TOKEN", "gho-from-env")
    assert resolve_github_token() == "gho-from-env"


def test_resolve_github_token_none_when_nothing_set(
    user_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from spc.userconfig import resolve_github_token

    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    import spc.githubapi as githubapi

    monkeypatch.setattr(githubapi.shutil, "which", lambda _: None)
    assert resolve_github_token() is None


def test_os_environ_still_read_directly_by_llm_module(
    user_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The legacy llm.resolve_api_key path stays for backward compat of imports."""
    import spc.llm as llm

    monkeypatch.setenv("SPC_API_KEY", "legacy")
    assert llm.resolve_api_key("SPC_API_KEY") == "legacy"


def test_load_repairs_world_readable_file(user_home: Path) -> None:
    """A pre-existing permissive user config is chmod'ed 600 on load (best-effort)."""
    user_home.parent.mkdir(parents=True, exist_ok=True)
    user_home.write_text('[llm]\napi_key = "sk-abcdef1234567890"\n', encoding="utf-8")
    user_home.chmod(0o644)
    config = load_user_config()
    assert config.llm_api_key == "sk-abcdef1234567890"
    assert stat.S_IMODE(user_home.stat().st_mode) == 0o600


def test_load_survives_chmod_failure(
    user_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """chmod failing (e.g. exotic filesystem) never breaks loading — fail soft."""
    user_home.parent.mkdir(parents=True, exist_ok=True)
    user_home.write_text('[llm]\napi_key = "sk-abcdef1234567890"\n', encoding="utf-8")

    def boom(*args: object, **kwargs: object) -> None:
        raise OSError("no chmod here")

    monkeypatch.setattr(Path, "chmod", boom)
    config = load_user_config()
    assert config.llm_api_key == "sk-abcdef1234567890"
