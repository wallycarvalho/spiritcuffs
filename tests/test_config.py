"""Config defaults, partial overrides, typo tolerance, and TOML writing."""

from __future__ import annotations

import tomllib
from pathlib import Path

from spc.config import (
    GithubConfig,
    SpcConfig,
    load_config,
    render_default_config,
    write_default_config,
)


def test_defaults_match_spec() -> None:
    config = SpcConfig()
    assert config.llm.base_url == "https://openrouter.ai/api/v1"
    assert config.llm.model == "anthropic/claude-3.5-haiku"
    assert config.llm.api_key_env == "SPC_API_KEY"
    assert config.review.show_nits is False
    assert config.review.strict is False
    assert config.review.max_diff_kb == 200
    assert config.review.cache_diff_hash is True
    assert config.secrets.block_on_secrets is False
    assert config.learn.learn_max_commits == 2000
    assert config.learn.profile_max_age_days == 30
    assert config.github.enabled == "auto"
    assert config.github.github_max_prs == 200
    assert config.github.github_max_comments == 1000
    assert config.github.github_pages_max == 10


def test_load_missing_file_returns_defaults(tmp_path: Path) -> None:
    config = load_config(tmp_path)
    assert config == SpcConfig()


def test_load_partial_override(tmp_path: Path) -> None:
    (tmp_path / ".spc.toml").write_text(
        "[review]\nstrict = true\nmax_diff_kb = 50\n[llm]\nmodel = \"my/model\"\n",
        encoding="utf-8",
    )
    config = load_config(tmp_path)
    assert config.review.strict is True
    assert config.review.max_diff_kb == 50
    assert config.review.show_nits is False  # untouched default
    assert config.llm.model == "my/model"
    assert config.llm.base_url == "https://openrouter.ai/api/v1"  # untouched default


def test_write_default_config_no_overwrite(tmp_path: Path) -> None:
    first = write_default_config(tmp_path)
    assert first is not None
    second = write_default_config(tmp_path)
    assert second is None  # did not clobber
    # The written file must round-trip through the loader.
    config = load_config(tmp_path)
    assert config == SpcConfig()


def test_rendered_default_is_parsable() -> None:
    data = tomllib.loads(render_default_config())
    assert data["llm"]["api_key_env"] == "SPC_API_KEY"
    assert data["github"]["enabled"] == "auto"
    assert data["llm"]["timeout"] == 30.0


def test_unknown_key_is_dropped_with_defaults_kept(tmp_path: Path) -> None:
    """A typo'd key must never crash loading — it's ignored, defaults fill in."""
    (tmp_path / ".spc.toml").write_text(
        "[llm]\ntimeot = 5\nmodel = \"my/model\"\n",
        encoding="utf-8",
    )
    config = load_config(tmp_path)
    assert config.llm.model == "my/model"  # valid sibling key still applies
    assert config.llm.timeout == 30.0  # typo'd key ignored; default intact


def test_wrong_typed_value_falls_back_to_default(tmp_path: Path) -> None:
    (tmp_path / ".spc.toml").write_text(
        '[review]\nmax_diff_kb = "200"\nstrict = "yes"\n',
        encoding="utf-8",
    )
    config = load_config(tmp_path)
    assert config.review.max_diff_kb == 200  # string, not int -> default
    assert config.review.strict is False  # string, not bool -> default


def test_bool_is_not_accepted_for_int_field(tmp_path: Path) -> None:
    """bool is an int subclass in Python; it must NOT pass as an int value."""
    (tmp_path / ".spc.toml").write_text("[review]\nmax_diff_kb = true\n", encoding="utf-8")
    assert load_config(tmp_path).review.max_diff_kb == 200


def test_unparsable_toml_falls_back_to_defaults(tmp_path: Path) -> None:
    (tmp_path / ".spc.toml").write_text("not [valid toml", encoding="utf-8")
    assert load_config(tmp_path) == SpcConfig()


def test_section_never_raises_on_any_garbage(tmp_path: Path) -> None:
    """The push path depends on load_config never raising."""
    (tmp_path / ".spc.toml").write_text(
        '[llm]\nbase_url = 123\n[review]\nshow_nits = "maybe"\n'
        "[learn]\nlearn_max_commits = [1, 2]\n",
        encoding="utf-8",
    )
    config = load_config(tmp_path)
    assert config == SpcConfig()  # every bad value fell back to default


def test_github_enabled_unquoted_bool_false_is_honored(tmp_path: Path) -> None:
    """enabled = false (TOML bool) must disable mining, not fall back to auto."""
    (tmp_path / ".spc.toml").write_text("[github]\nenabled = false\n", encoding="utf-8")
    assert load_config(tmp_path).github.enabled == "false"


def test_github_enabled_unquoted_bool_true_is_honored(tmp_path: Path) -> None:
    (tmp_path / ".spc.toml").write_text("[github]\nenabled = true\n", encoding="utf-8")
    assert load_config(tmp_path).github.enabled == "true"


def test_github_enabled_garbage_falls_back_to_auto(tmp_path: Path) -> None:
    (tmp_path / ".spc.toml").write_text('[github]\nenabled = "maybe"\n', encoding="utf-8")
    assert load_config(tmp_path).github.enabled == "auto"


def test_github_config_roundtrip(tmp_path: Path) -> None:
    config = SpcConfig(
        github=GithubConfig(enabled="false", base_url="https://ghe.corp/api/v3")
    )
    (tmp_path / ".spc.toml").write_text(
        '[github]\nenabled = "false"\nbase_url = "https://ghe.corp/api/v3"\n',
        encoding="utf-8",
    )
    assert load_config(tmp_path).github == config.github


# --- user-config precedence (repo .spc.toml > ~/.spc/config.toml > defaults) ---


def _write_user_config(
    tmp_path: Path,
    *,
    llm_base_url: str | None = None,
    llm_model: str | None = None,
    llm_api_key_env: str | None = None,
    llm_api_key: str | None = None,
) -> None:
    from spc.userconfig import save_user_config, user_config_path

    save_user_config(
        user_config_path(),
        llm_base_url=llm_base_url,
        llm_model=llm_model,
        llm_api_key_env=llm_api_key_env,
        llm_api_key=llm_api_key,
    )


def test_user_config_fills_defaults_when_repo_missing(tmp_path: Path) -> None:
    _write_user_config(
        tmp_path,
        llm_base_url="https://api.openai.com/v1",
        llm_model="gpt-4o-mini",
        llm_api_key_env="OPENAI_API_KEY",
    )
    config = load_config(tmp_path)
    assert config.llm.base_url == "https://api.openai.com/v1"
    assert config.llm.model == "gpt-4o-mini"
    assert config.llm.api_key_env == "OPENAI_API_KEY"
    assert config.review.strict is False  # non-llm sections unaffected


def test_repo_config_beats_user_config(tmp_path: Path) -> None:
    _write_user_config(tmp_path, llm_model="gpt-4o-mini")
    (tmp_path / ".spc.toml").write_text(
        '[llm]\nmodel = "my/model"\n', encoding="utf-8"
    )
    assert load_config(tmp_path).llm.model == "my/model"


def test_user_config_ignored_for_repo_explicit_defaults(tmp_path: Path) -> None:
    """Repo values equal to the built-in default count as unset — user config fills.

    `spc init` writes every key with defaults, so a generated .spc.toml must
    not permanently mask the user's setup choices.
    """
    _write_user_config(tmp_path, llm_model="gpt-4o-mini")
    (tmp_path / ".spc.toml").write_text(
        '[llm]\nmodel = "anthropic/claude-3.5-haiku"\n', encoding="utf-8"
    )
    assert load_config(tmp_path).llm.model == "gpt-4o-mini"


def test_user_config_partial_fill_keeps_repo_partial_override(tmp_path: Path) -> None:
    _write_user_config(tmp_path, llm_base_url="https://api.openai.com/v1")
    (tmp_path / ".spc.toml").write_text(
        '[llm]\nmodel = "my/model"\n', encoding="utf-8"
    )
    llm = load_config(tmp_path).llm
    assert llm.model == "my/model"  # repo choice
    assert llm.base_url == "https://api.openai.com/v1"  # user fill


def test_llm_api_key_field_loaded_from_repo_toml(tmp_path: Path) -> None:
    (tmp_path / ".spc.toml").write_text(
        '[llm]\napi_key = "sk-repo"\n', encoding="utf-8"
    )
    assert load_config(tmp_path).llm.api_key == "sk-repo"
