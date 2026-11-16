# STEP 3 Report — Onboarding Wizard + Release Prep

Date: 2026-11-16 · Branch: main · Tests: **195 passed**, ruff clean · Build: wheel + sdist verified

## What was built

### 1. `spc setup` — interactive onboarding wizard (new: `spc/setup.py`, command in `spc/cli.py`)

Opencode-style wizard using rich prompts (no new dependencies):

1. **Welcome panel** — 2-sentence what-it-does.
2. **Provider selection** — curated table (`spc/setup.py` `PROVIDERS`):
   | key | base_url | default model | key env |
   |---|---|---|---|
   | openrouter | https://openrouter.ai/api/v1 | anthropic/claude-3.5-haiku | OPENROUTER_API_KEY |
   | openai | https://api.openai.com/v1 | gpt-4o-mini | OPENAI_API_KEY |
   | anthropic | https://api.anthropic.com/v1 (OpenAI-compat) | claude-3-5-haiku-latest | ANTHROPIC_API_KEY |
   | ollama | http://localhost:11434/v1 | llama3.1 | LLM_API_KEY |
   plus "keep current" (falls back to stored user config / built-in defaults).
3. **API key** — hidden input (typer `hide_input`); two storage paths:
   - paste key → `~/.spc/config.toml` (`llm.api_key`), file chmod **0600** on every write;
   - or store env-var **name only** (`llm.api_key_env`) — key never touches disk.
   Key is masked in all output (`sk-…abc123` style, `spc/userconfig.py::mask_secret`),
   never printed in full, never written to the repo-committed `.spc.toml`, never
   enters `.spc/` artifacts.
4. **Model picker** — curated 3–4 per provider + free "keep current" entry; no
   model-listing API calls (offline).
5. **GitHub token (optional)** — same storage rules → user config `github.token`
   (mode 600, masked). Feeds step-2 PR mining.
6. **Review preferences** — strict / show_nits y/n → written into repo
   `.spc.toml` `[review]` **only if the file exists** (never created by setup).
7. **Test connection** — one cheap live call ("ping", timeout 15 s) through the
   real `spc/llm.py` plumbing; masked success/failure, exit 0 either way.
8. **Repo wiring** — in a git repo: offers `spc init`; otherwise prints
   `cd <repo> && spc init`.

**Non-interactive mode** (`--non-interactive` / `-y`): `--provider`, `--api-key`,
`--api-key-env`, `--model`, `--base-url`, `--github-token`, `--github-token-env`,
`--strict/--no-strict`, `--nits/--no-nits`, `--skip-ping`. Missing values fall
back to stored config/defaults — idempotent, CI-friendly.

**Never crashes**: `KeyboardInterrupt`, `EOFError`, and typer `Abort` → clean
"setup cancelled" exit 0; broad except → one-line warning, exit 0.

### 2. User-level config + precedence (new: `spc/userconfig.py`, changes in `spc/config.py`)

- `~/.spc/config.toml` (path overridable via `SPC_USER_CONFIG` for tests), mode
  0600 on create and on every write; merge-save never wipes unrelated keys.
- Resolution order (implemented + documented in README):
  **repo `.spc.toml` → user `~/.spc/config.toml` → built-in defaults**; API key:
  repo `llm.api_key` → user `llm.api_key` → env var (repo `api_key_env` if
  explicitly set, else user env name, else `SPC_API_KEY`).
- `load_config` merges user config only into fields still at built-in defaults
  (repo wins when it differs; a repo value *equal to the default* defers to user
  config, because `spc init` writes every key with defaults).
- Call sites rewired: `spc/review.py`, `spc/profile.py`, `spc/githubhistory.py`
  resolve keys via `userconfig.resolve_api_key(config.llm)`; GitHub token:
  user config `github.token` → existing `GITHUB_TOKEN` → `GH_TOKEN` → `gh auth token`.

### 3. Release prep

- **pyproject.toml**: full metadata — name `spiritcuffs` 0.1.0, MIT +
  `license-files`, authors, requires-python >=3.11, classifiers, keywords,
  `[project.urls]` Homepage/Repository/Issues/Changelog, deps restored under
  `[project]` (a bad insert had left them under `[project.urls]` — caught by
  `uv build`).
- **LICENSE**: MIT, "SpiritCuffs contributors".
- **CHANGELOG.md**: Keep-a-Changelog, `[0.1.0] - 2026-11-16`.
- **npm wrapper**: `npm/package.json` (bin `spiritcuffs` + `spc`, engines
  node>=18, no deps) + `npm/bin/cli.js` (~50 lines): probes `uvx` then `pipx`,
  spawns `uvx spiritcuffs@latest <args>` (or `pipx run spiritcuffs <args>`)
  forwarding stdio/exit codes; missing runner → one-line uv install hint, exit 1.
- **README**: 30-second quickstart (uvx/pipx/npx + `spc setup` first), command
  table, privacy section (keys in mode-600 user config, masked), config
  reference with `llm.api_key` + key-resolution order, FAQ/troubleshooting
  (no key → warning exit 0; strict + `--no-verify`; stale profile; CI;
  Windows), roadmap updated (npm wrapper shipped in v1).
- **Build verification**: `uv build` → `dist/spiritcuffs-0.1.0.tar.gz` +
  `-py3-none-any.whl`; wheel installed into a scratch venv
  (`uv pip install`), `spc version` → `spiritcuffs 0.1.0`, `spc setup --help`,
  `spc history --help` all render. **Nothing was published.**

## Fixes found by smoke-testing the wizard

- **Stdin EOF / Ctrl-D mid-wizard** surfaced as typer `Abort` with an empty
  message → caught by the broad handler as "setup hit an error ()". Now
  `except (KeyboardInterrupt, EOFError, typer.Abort)` → friendly cancel, exit 0.
- **Comment-stripping rewrite**: preferences step originally parsed `.spc.toml`
  and rewrote it with `tomli_w.dumps` — this silently deleted every inline
  comment (caught when a smoke run desecrated this repo's own committed
  `.spc.toml`). Replaced with `_update_toml_section` — a targeted line edit
  inside `[review]` that preserves comments (even the edited line's own trailing
  comment), appends missing keys at section end, and refuses to touch a config
  that fails `tomllib` parsing.

## Tests (206 total; +58 vs step 2's 148)

- `tests/test_userconfig.py` (19): path override, missing/garbage → empty,
  mode 600, merge-never-wipe, roundtrip, key-resolution arms (repo key → user key
  → env, custom env names), mask rules, GitHub-token resolution + env fallback.
- `tests/test_config.py` (+5): user fills defaults when repo missing; repo beats
  user; default-equal repo defers to user; partial fills coexist; repo
  `llm.api_key` loads.
- `tests/test_setup.py` (18): provider table, non-interactive E2E (mock ping),
  masking in output, key never in repo config, idempotent re-run, unknown
  provider fallback, preferences written/skipped, ping success/failure exit 0,
  KeyboardInterrupt exit 0, GitHub token stored masked, not-in-git-repo message,
  init wiring, comment-preservation regression, missing-key append.
- `tests/test_npm_wrapper.py` (3, skipif no node): arg+exit-code forwarding vs a
  fake `uvx`, missing-runner hint exit 1, pipx fallback (`run spiritcuffs …`).
- npm wrapper also manually smoke-tested with real node v24 + fake uvx on PATH
  (exit code 7 forwarded; hint path with runner stripped from PATH).

## Dogfood

Wizard smoke-tested against this repo with real stdin twice (env-var path and
paste-key path; mode 600 verified via `stat`; masked output verified; connection
test fails soft on a bogus key with exit 0). `.spc.toml` restored after the
comment-stripping incident (now regression-tested).

## Deviations / notes for the lead

1. **Placeholder URLs** — `https://github.com/spiritcuffs/spiritcuffs` in
   `pyproject.toml [project.urls]` and `npm/package.json` repo field. Replace
   with the real repo URL before publishing.
2. **Keyring/keyring storage** — out of scope per instructions; noted as future
   work in README roadmap (v1.1).
3. **Provider model defaults are curated, not fetched** — no model-listing API
   calls (per spec); shortlists will age and may need occasional bumps.
4. **Anthropic endpoint** — points at `https://api.anthropic.com/v1`
   (OpenAI-compatible); no live test against a real key was possible (no keys on
   this machine) — connection test is the user's verification step.
5. **`default-equal defers to user` precedence** — a repo value identical to the
   built-in default is treated as "unset" so `spc init`'s full-file write doesn't
   mask the user's setup choices; documented in README (config reference).
6. **npm package version stays 0.0.0** — it's a thin launcher; bump to 0.1.0 at
   real npm publish time if desired.

## Review fixes (post-review hardening pass)

All 7 findings fixed; suite went 195 → 206.

1. **`--api-key-env` was a silent no-op in non-interactive mode** (MAJOR) —
   `_step_api_key` (spc/setup.py) now persists the env-var name when provided
   with `--api-key-env`, even without `--api-key`. README's CI recipe
   (`spc setup -y --provider openrouter --api-key-env VAR`) now configures
   something. Test: `test_api_key_env_persisted_in_non_interactive_mode`.
2. **Pre-existing permissive user config is now repaired on load** (MAJOR) —
   `load_user_config` (spc/userconfig.py) best-effort `chmod(0o600)`s the file
   whenever it exists; `OSError` is swallowed (filesystems without chmod).
   Tests: `test_load_repairs_world_readable_file`,
   `test_load_survives_chmod_failure`.
3. **Non-interactive setup no longer auto-runs `spc init`** (MAJOR) —
   `_offer_repo_wiring` prints `wire up this repo later: spc init` instead of
   spending LLM tokens in CI. Interactive confirm flow unchanged.
   Tests: `test_non_interactive_inside_git_repo_does_not_auto_init`,
   `test_repo_wiring_interactive_confirm_runs_init_inside_git_repo` (renamed
   from `test_repo_wiring_runs_init_inside_git_repo`).
4. **API key can leak into error text** (MINOR) — all `LlmError` messages are
   redacted via `_redact()` (spc/llm.py): the configured key is replaced with
   `***` before any body/exception text is embedded. A hostile endpoint can no
   longer echo the `Authorization` value through a ping failure.
   Tests: tests/test_llm.py (new file — redaction on 4xx, on retry ladder,
   no-op with no key).
5. **`_update_toml_section` comment split was quote-unaware** (MINOR) —
   replaced the first-`#` split with `_split_trailing_comment`, which tracks
   quotes so `api_key_env = "VAR # not comment"  # trailing` survives a
   rewrite verbatim. Test:
   `test_preferences_preserve_comment_with_hash_in_string`.
6. **Ping ignored repo-level `llm.api_key`** (MINOR) — `_ping` now loads the
   repo config and resolves the key exactly like the review path
   (`resolve_api_key(config.llm)`); a repo with a committed key no longer gets
   "connection test failed" while reviews succeed. Test:
   `test_ping_uses_repo_api_key`.
7. **Ping could stall ~48s on a hanging endpoint** (MINOR) —
   `llm.complete()` gained an `attempts` parameter (default unchanged for the
   review path); `_ping` passes `attempts=1`, so worst case is one 15s timeout.
   Tests: `attempts=1` makes exactly one HTTP call; default still retries 3.

Ruff clean (spc + tests); full suite 206 passed.

## What's NOT in this step (by design)

- No publishing (no `twine upload`, no `npm publish`).
- No keychain/keyring integration (roadmap).
- No model-listing API calls.
