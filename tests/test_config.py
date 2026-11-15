"""Config defaults, partial overrides, typo tolerance, and TOML writing."""

from __future__ import annotations

import tomllib
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
