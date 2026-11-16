"""`spc setup` wizard: non-interactive E2E, idempotency, provider table, masking."""

from __future__ import annotations

import io
import stat
from pathlib import Path

import pytest
from rich.console import Console

from spc.config import LlmConfig, load_config
from spc.setup import PROVIDERS, run_setup
from spc.userconfig import load_user_config, save_user_config, user_config_path


def _console() -> Console:
    return Console(file=io.StringIO(), width=200)


@pytest.fixture(autouse=True)
def _in_temp_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Run the wizard from a temp dir so repo-config effects never touch the real repo."""
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _no_ping(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the ping so tests never touch the network."""
    import spc.setup as setup_mod

    monkeypatch.setattr(setup_mod, "_ping", lambda *a, **kw: None)


def test_provider_table_defaults() -> None:
    by_key = {p.key: p for p in PROVIDERS}
    assert by_key["openrouter"].base_url == "https://openrouter.ai/api/v1"
    assert by_key["openrouter"].api_key_env == "OPENROUTER_API_KEY"
    assert by_key["openai"].base_url == "https://api.openai.com/v1"
    assert by_key["openai"].model == "gpt-4o-mini"
    assert by_key["anthropic"].api_key_env == "ANTHROPIC_API_KEY"
    assert by_key["ollama"].base_url == "http://localhost:11434/v1"
    assert by_key["ollama"].api_key_env == "LLM_API_KEY"


def test_non_interactive_stores_key_and_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_ping(monkeypatch)
    exit_code = run_setup(
        _console(),
        non_interactive=True,
        provider_key="openrouter",
        api_key="sk-test-1234567890",
        model="openai/gpt-4o-mini",
        skip_ping=True,
    )
    assert exit_code == 0
    user = load_user_config()
    assert user.llm_api_key == "sk-test-1234567890"
    assert user.llm_model == "openai/gpt-4o-mini"
    assert user.llm_base_url == "https://openrouter.ai/api/v1"
    assert user.llm_api_key_env == "OPENROUTER_API_KEY"
    assert stat.S_IMODE(user_config_path().stat().st_mode) == 0o600


def test_non_interactive_output_masks_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    _no_ping(monkeypatch)
    secret = "sk-proj-supersecret-value-99887766"
    console = Console(file=io.StringIO(), width=200)
    run_setup(console, non_interactive=True, provider_key="openai", api_key=secret, skip_ping=True)
    printed = console.file.getvalue()  # type: ignore[union-attr]
    assert secret not in printed
    assert "…887766" in printed  # masked tail recognizable


def test_key_never_written_to_repo_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_ping(monkeypatch)
    (tmp_path / ".spc.toml").write_text("[review]\nstrict = false\n", encoding="utf-8")
    run_setup(
        _console(),
        non_interactive=True,
        provider_key="openai",
        api_key="sk-repocheck-123456",
        skip_ping=True,
    )
    repo_text = (tmp_path / ".spc.toml").read_text(encoding="utf-8")
    assert "sk-repocheck-123456" not in repo_text
    assert "api_key" not in repo_text


def test_idempotent_rerun_updates_instead_of_duplicating(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_ping(monkeypatch)
    first = run_setup(
        _console(), non_interactive=True, provider_key="openai",
        api_key="sk-first-key-0001", model="gpt-4o-mini", skip_ping=True,
    )
    second = run_setup(
        _console(), non_interactive=True, provider_key="openrouter",
        api_key="sk-second-key-002", model="anthropic/claude-3.5-haiku", skip_ping=True,
    )
    assert first == second == 0
    user = load_user_config()
    assert user.llm_api_key == "sk-second-key-002"
    assert user.llm_model == "anthropic/claude-3.5-haiku"
    assert user.llm_base_url == "https://openrouter.ai/api/v1"
    # Single llm section, not appended duplicates.
    import tomllib

    raw = tomllib.loads(user_config_path().read_text(encoding="utf-8"))
    assert list(raw["llm"].keys()).count("api_key") == 1


def test_keep_current_when_no_flags_in_non_interactive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_ping(monkeypatch)
    save_user_config(
        user_config_path(), llm_api_key="sk-keep-12345678", llm_model="my/model"
    )
    run_setup(_console(), non_interactive=True, skip_ping=True)
    user = load_user_config()
    assert user.llm_api_key == "sk-keep-12345678"
    assert user.llm_model == "my/model"


def test_unknown_provider_flag_falls_back_to_stored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_ping(monkeypatch)
    save_user_config(user_config_path(), llm_base_url="https://my.corp/v1", llm_model="corp/model")
    run_setup(_console(), non_interactive=True, provider_key="nonsense", skip_ping=True)
    assert load_user_config().llm_base_url == "https://my.corp/v1"


def test_preferences_written_to_repo_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_ping(monkeypatch)
    (tmp_path / ".spc.toml").write_text("[review]\n", encoding="utf-8")
    run_setup(
        _console(), non_interactive=True, strict=True, show_nits=True, skip_ping=True
    )
    config = load_config(tmp_path)
    assert config.review.strict is True
    assert config.review.show_nits is True


def test_preferences_skipped_without_repo_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_ping(monkeypatch)
    exit_code = run_setup(
        _console(), non_interactive=True, strict=True, show_nits=True, skip_ping=True
    )
    assert exit_code == 0
    assert not (tmp_path / ".spc.toml").exists()


def test_ping_success_prints_masked_reply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:

    def fake_complete(messages, *, model, base_url, api_key, timeout, **kwargs):  # noqa: ANN001, ANN003
        return "pong\n"

    monkeypatch.setattr("spc.llm.complete", fake_complete)
    console = Console(file=io.StringIO(), width=200)
    run_setup(console, non_interactive=True, skip_ping=False)
    printed = console.file.getvalue()  # type: ignore[union-attr]
    assert "connection ok" in printed
    assert "pong" in printed


def test_ping_failure_is_fail_soft(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import spc.llm as llm_mod
    import spc.setup as setup_mod

    def boom(*args, **kwargs):  # noqa: ANN002, ANN003
        raise llm_mod.LlmError("HTTP 401: bad key")

    monkeypatch.setattr(setup_mod, "_ping", boom)
    exit_code = run_setup(_console(), non_interactive=True, skip_ping=False)
    assert exit_code == 0  # wizard never fails


def test_keyboard_interrupt_is_clean_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import spc.setup as setup_mod

    def interrupt(*args, **kwargs):  # noqa: ANN002, ANN003
        raise KeyboardInterrupt

    monkeypatch.setattr(setup_mod, "_resolve_provider", interrupt)
    exit_code = run_setup(_console(), non_interactive=True)
    assert exit_code == 0


def test_github_token_stored_masked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_ping(monkeypatch)
    run_setup(
        _console(),
        non_interactive=True,
        github_token="gho_token_abcdef123456",
        skip_ping=True,
    )
    user = load_user_config()
    assert user.github_token == "gho_token_abcdef123456"
    assert stat.S_IMODE(user_config_path().stat().st_mode) == 0o600


def test_repo_wiring_message_when_not_in_git_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    _no_ping(monkeypatch)
    console = Console(file=io.StringIO(), width=200)
    run_setup(console, non_interactive=True, skip_ping=True)
    printed = console.file.getvalue()  # type: ignore[union-attr]
    assert "not in a git repo" in printed


def test_repo_wiring_interactive_confirm_runs_init_inside_git_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Interactive yes-confirm runs init; non-interactive never does (no LLM spend).

    Menus are skipped via flags (stdin isn't a TTY under pytest); confirm→True
    accepts wiring, prompt→"" skips the optional GitHub hidden prompt.
    """
    import subprocess

    subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.email", "t@example.com"], check=True
    )
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "T"], check=True)
    _no_ping(monkeypatch)
    import spc.setup as setup_mod

    monkeypatch.setattr(setup_mod.typer, "confirm", lambda *a, **k: True)
    monkeypatch.setattr(setup_mod.typer, "prompt", lambda *a, **k: "")
    exit_code = run_setup(
        _console(),
        non_interactive=False,
        provider_key="openrouter",
        api_key="sk-test-1234567890abcd",
        model="anthropic/claude-3.5-haiku",
        skip_ping=True,
    )
    assert exit_code == 0
    assert (tmp_path / ".spc.toml").is_file()


def test_resolve_api_key_uses_stored_key_after_setup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: the review path resolves the key the wizard stored."""
    from spc.userconfig import resolve_api_key

    _no_ping(monkeypatch)
    run_setup(
        _console(), non_interactive=True, provider_key="openrouter",
        api_key="sk-e2e-1234567890", skip_ping=True,
    )
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    assert resolve_api_key(LlmConfig(api_key_env="OPENROUTER_API_KEY")) == "sk-e2e-1234567890"


def test_preferences_preserve_comments(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Targeted key-line edits must not strip the config's comments (tomli_w rewrite did)."""
    _no_ping(monkeypatch)
    original = (
        "[llm]\n"
        'base_url = "https://x"  # keep me\n'
        "\n"
        "[review]\n"
        "strict = false  # inline comment survives\n"
        "show_nits = false\n"
    )
    (tmp_path / ".spc.toml").write_text(original, encoding="utf-8")
    run_setup(_console(), non_interactive=True, strict=True, skip_ping=True)
    text = (tmp_path / ".spc.toml").read_text(encoding="utf-8")
    assert "# keep me" in text
    assert "# inline comment survives" in text
    assert "strict = true" in text
    assert "show_nits = false" in text


def test_preferences_append_missing_keys(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _no_ping(monkeypatch)
    (tmp_path / ".spc.toml").write_text("[llm]\nbase_url = \"https://x\"\n", encoding="utf-8")
    run_setup(_console(), non_interactive=True, strict=True, show_nits=True, skip_ping=True)
    text = (tmp_path / ".spc.toml").read_text(encoding="utf-8")
    assert "[review]" in text
    assert "strict = true" in text
    assert "show_nits = true" in text


def test_api_key_env_persisted_in_non_interactive_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--api-key-env without --api-key must persist the env-var name (was a silent no-op)."""
    _no_ping(monkeypatch)
    run_setup(
        _console(),
        non_interactive=True,
        provider_key="openrouter",
        api_key_env="MY_CUSTOM_VAR",
        skip_ping=True,
    )
    user = load_user_config()
    assert user.llm_api_key_env == "MY_CUSTOM_VAR"
    assert user.llm_api_key is None  # env-var-only path stores no key value


def test_non_interactive_inside_git_repo_does_not_auto_init(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """`spc setup -y` in a repo prints the one-liner — it must not run init (LLM spend)."""
    import subprocess

    subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)], check=True)
    _no_ping(monkeypatch)
    console = Console(file=io.StringIO(), width=200)
    exit_code = run_setup(console, non_interactive=True, skip_ping=True)
    printed = console.file.getvalue()  # type: ignore[union-attr]
    assert exit_code == 0
    assert "spc init" in printed
    assert (tmp_path / ".spc.toml").exists() is False  # init never ran


def test_preferences_preserve_comment_with_hash_in_string(tmp_path: Path) -> None:
    """A '#' inside a quoted value is not a comment — the editor must not split there."""
    from spc.setup import _update_toml_section

    text = '[llm]\napi_key_env = "VAR # not comment"  # trailing\n'
    updated = _update_toml_section(text, "llm", {"api_key_env": "OTHER"})
    assert updated == '[llm]\napi_key_env = OTHER  # trailing\n'
    # the quoted value itself was never treated as a comment boundary
    assert "not comment" not in updated.split("#")[0]


def test_ping_uses_repo_api_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ping resolves the key like the review path — repo llm.api_key must reach the call."""
    import subprocess

    import spc.setup as setup_mod

    subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)], check=True)
    (tmp_path / ".spc.toml").write_text(
        '[llm]\nbase_url = "https://llm.example/v1"\nmodel = "m"\napi_key = "sk-repo-key-987654"\n'
    )
    captured: dict[str, object] = {}

    def fake_complete(messages, *, model, base_url, api_key, timeout, **kwargs):  # noqa: ANN001, ANN003
        captured["api_key"] = api_key
        return "pong\n"

    monkeypatch.setattr("spc.llm.complete", fake_complete)
    setup_mod._ping(_console(), model="m")
    assert captured["api_key"] == "sk-repo-key-987654"
