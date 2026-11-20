# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.3.0] - 2026-11-20

### Changed

- build secret fixtures at runtime so no matchable literal is committed
- Enhance tests for learning and secrets management

## [0.2.0] - 2026-11-17

### Added

- enhance pre-push review functionality and improve diff handling

### Fixed

- allowlist AWS documentation example keys

### Changed

- Add image to README for visual enhancement

## [0.1.2] - 2026-11-16

### Added

- dynamic model list in setup wizard (live provider listing, chat-completions filter, shortlist fallback)
- release automation script (bump + changelog) with tests

### Fixed

- make dynamic model listing actually work on all providers

### Changed

- remove npm wrapper — distribution is uv/pipx only

### Docs

- revert README to bare uvx/spiritcuffs form now that the alias is published

## [0.1.1] - 2026-11-16

### Fixed

- Added `spiritcuffs` executable alias so `uvx spiritcuffs` and
  `pipx run spiritcuffs` work with the bare package name, matching `npx`.
  (`uvx spiritcuffs` previously failed: the package only shipped `spc`.)

## [0.1.0] - 2026-11-16

First public release: a local, LLM-powered second opinion on every `git push`.

### Fixed

- With `provider = "copilot"`, `spc learn` (profile synthesis + PR theme
  clustering) sent requests without a Copilot token and failed with HTTP 400.
  Provider-aware LLM resolution now lives in one shared place
  (`spc/provider.py`) used by reviews, learn, clustering, and the setup
  connection test alike.

### Added

- `spc init` — one-command wiring: writes `.spc.toml` (committed, team-shared),
  installs the git `pre-push` hook (`spc hook --remove` to uninstall),
  discovers convention files, mines git history, synthesizes
  `.spc/profile.md` (one LLM call), and runs the first review immediately.
- `spc review` — reviews the pending diff (`@{upstream}...HEAD`, staged
  fallback) against a tight rubric: critical = bugs/logic/security/secrets/
  data-loss; warns for missing error handling, untested risky paths, perf
  footguns. Findings capped at 10, highest severity first. Fail-soft: exit 0
  unless `strict = true` and a critical finding exists.
- `spc learn` — git-history mining: hotspots, past bug classes, test
  co-change, language mix; watermarked, LLM-synthesized profile
  (`.spc/profile.md`), raw stats in gitignored `.spc/evidence.json`.
- GitHub mining (step 2) — merged PRs + inline review comments distilled into
  recurring review themes. Watermark-incremental, rate-limit and API-budget
  aware, strictly fail-soft. Only synthesized themes enter the profile; raw
  comments stay local in `.spc/evidence.json`. GitHub Enterprise supported
  (`[github] base_url`).
- `spc setup` — interactive onboarding wizard: provider picker (OpenRouter,
  OpenAI, Anthropic, GitHub Copilot, local endpoints), hidden API-key entry,
  GitHub Copilot device-flow login (stores only the long-lived OAuth token;
  the per-run API token is never persisted), model shortlist, optional GitHub
  token, review preferences, live connection test, and an offer to wire the
  current repo. Non-interactive mode (`--non-interactive` + flags, incl.
  `--copilot-token`) for scripted/CI use. Idempotent — re-runs edit existing
  choices.
- User-level config `~/.spc/config.toml` (mode 0600) — stores the LLM API key
  and GitHub token outside any repo. Resolution order: repo `.spc.toml`
  `llm.api_key` → user config → env var. Keys are masked in all output and
  never written to the committed repo config.
- Config precedence: repo `.spc.toml` > user `~/.spc/config.toml` > built-in
  defaults.
- `spc history` — pretty-printed review runs (JSONL, local only).
- Local secrets regex scan on every diff before any LLM call
  (`block_on_secrets = true` skips the LLM entirely).
- Diff-hash cache — pushing the same diff twice reprints cached findings
  without an LLM call (cache salted with model/strict/show_nits/profile).
- Diff truncation with a dropped-files list (never silently truncated).
- npm wrapper (`npx spiritcuffs`) — resolves `uvx` or `pipx` and forwards
  argv/stdio/exit codes; prints a one-line install hint otherwise.
  *(removed post-0.1.1 — distribution is uv/pipx only; restore from git
  history if npm returns)*
- Token safety: the GitHub token is attached only to `api.github.com` or the
  origin remote's own host (GHE), https-only — never to a repo-committed
  `base_url` pointing elsewhere.

[0.1.0]: https://github.com/spiritcuffs/spiritcuffs/releases/tag/v0.1.0
