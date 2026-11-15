"""Config defaults, partial overrides, and TOML writing."""

from __future__ import annotations

from pathlib import Path

from spc.config import SpcConfig, load_config, render_default_config, write_default_config


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
    import tomllib

    data = tomllib.loads(render_default_config())
    assert data["llm"]["api_key_env"] == "SPC_API_KEY"
    assert data["github"]["enabled"] == "auto"
