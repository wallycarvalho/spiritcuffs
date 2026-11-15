# SpiritCuffs — Step 1 Report

**Date:** 2026-11-15 · **Status:** complete · **Tests:** 70 passed · **Lint:** ruff clean (`spc/` + `tests/`)

## What works (end to end)

| Command | Behavior | Verified |
|---|---|---|
| `spc init` | Detects project type (pyproject.toml / package.json / go.mod / Cargo.toml), writes `.spc.toml` (never clobbers an existing one), installs the pre-push hook (idempotent), ignores `.spc/` (`.gitignore` preferred, else `.git/info/exclude`), discovers convention files, runs `learn`, then runs the **first review immediately** | ✅ dogfood + tests |
| `spc learn` | Mines local git history (hotspots, bug classes, test co-change, languages) → `.spc/evidence.json` → ONE LLM call synthesizes `.spc/profile.md` (human-readable, editable). On LLM failure: one-line warning, exit 0, **no fallback profile** | ✅ dogfood (real + mock LLM) + tests |
| `spc review` | `@{upstream}...HEAD` diff → staged-diff fallback → silent exit 0 if both empty; stale-profile notice (never auto-refresh); diff-hash cache (no LLM call on identical diff); secrets regex scan; prompt with profile + conventions + per-language hints + rubric; strict JSON findings; rich output; exit 1 only when `strict = true` **and** a critical exists | ✅ tests + dogfood |
| `spc hook` / `spc hook --remove` | Marker-comment block install; remove strips the block and unlinks the file when nothing executable remains (bare shebang/comments are cleaned up); foreign hook content is preserved | ✅ tests |
| `spc history` | Rich table of recent runs from `~/.spc/history/<repo>-<pathhash8>.jsonl` | ✅ tests + dogfood |

## Dogfood results (this repo: `/Users/allysonalves/Projects/Pessoal/review-app`)

The directory was **not a git repo** — per the spec's fallback I initialized git and
made an initial commit, then ran against it:

1. `uv run spc init` → project type `python`, `.spc.toml` written, hook installed,
   `.spc/` ignored, 1 commit mined, synthesis **failed soft** (no `SPC_API_KEY` set →
   expected one-line warning, exit 0, no profile file). First review also failed soft
   (401 from OpenRouter, warning printed, exit 0, run logged to history).
2. `uv run spc review` with a staged README tweak → fail-soft LLM warning, exit 0,
   JSONL row appended (`staged`, 1 file, model recorded).
3. Full pipeline proven offline with `scripts/mock_llm_server.py` (a ~50-line mock
   OpenAI-compatible endpoint on localhost): `spc learn` mined, called the mock,
   and wrote a real `.spc/profile.md`. `.spc.toml` was restored to the real
   `openrouter` `base_url` afterward.
4. Real-LLM caveat: no API key in this environment, so the profile text shown is the
   mock's canned string. The synthesis prompt itself is covered by tests (stats only —
   never file contents or secrets).

## What was tested (70 tests, all green)

- **diff**: upstream→staged fallback, empty diff, changed-file parsing, hash,
  truncation with dropped-file list, pathhash8.
- **githistory**: hotspots, bug-class counting (incl. dual-keyword subjects),
  test co-change (a fixture commit touching src+tests), lockfile/vendor exclusion,
  language proportions, empty/non-repo → None. Regression test for the NUL-separated
  `git log --format=%x00%s` parser (blank-line parsing mixed subjects into file lists).
- **profile**: evidence roundtrip, staleness math (40-day-old file, boundary).
- **review**: finding parsing incl. fenced JSON, `{"findings": [...]}`, malformed
  output rescue, alias severities, capping at 10; prompt composition (profile,
  conventions, hotspots, dropped files listed as NOT reviewed); strict-blocks and
  strict-passes exit codes; LLM failure fail-soft; `block_on_secrets` skips the LLM.
- **secrets**: AWS key, GitHub token (literal), private key, JWT, generic assignment;
  context/removed lines ignored; short values ignored.
- **hook**: install idempotency, foreign-hook preservation, remove (block-only,
  full cleanup, no-op).
- **config**: spec defaults, partial overrides, no-clobber, rendered TOML parses.
- **log**: filename collision safety, roundtrip, latest-record-for-hash, cache-hit
  e2e (second identical diff → zero LLM calls), cache-disabled reruns, corrupt lines.
- **cli** (CliRunner): `init` full flow + rerunnability, `hook --remove`,
  `history`, `version`, review outside a repo → exit 0, exclude-file fallback.
- All LLM calls are mocked in tests; **no network** is ever touched.

## Deviations from the spec (with reasons)

1. **`tomli` not used for reading** — Python 3.11+ has stdlib `tomllib`; only
   `tomli-w` (writing) is a dependency, as permitted.
2. **Commit-style mining cut** — honored, not implemented.
3. **`github.enabled = "auto"`** — parsed into config (and rendered commented-out in
   `.spc.toml`) but deliberately unused; no `gh`/token code exists in step 1.
4. **Hook removal semantics** — the spec didn't say whether to delete the hook file;
   we strip our block and unlink the file only when nothing executable remains
   (a bare `#!/bin/sh` is dead scaffolding). Documented in `spc/hook.py`.
5. **Windows note** — the hook block and its tests carry the "runs via sh (Git for
   Windows ships bash for hooks)" note rather than silence.

## What step 2 plugs into

- **Evidence layer**: `spc/profile.py:save_evidence` writes `{"schema": 1, "source":
  "git", ...}` — a second source writes `{"source": "github", ...}` records beside it.
- **Prompt integration point**: `spc/profile.py:build_profile(evidence, config, *,
  extra_evidence_md="")` already accepts an optional extra-evidence markdown section
  (currently always `""` from `spc/learn.py`). Step 2 renders mined PR review comments
  into that section — no signature change needed.
- **Config**: `github.*` keys already parse and are rendered in `.spc.toml`.
- **Mining pattern to follow**: `spc/githistory.py` (GitEvidence dataclass +
  `mine_git_history`) is the template for a `githubhistory.py` returning a parallel
  evidence object.
