<img width="500" alt="yuyu" src="https://github.com/user-attachments/assets/37cf392e-c7f8-43b6-b65d-7f43c693c238" />



# SpiritCuffs (`spc`)

An LLM-powered second opinion on every `git push` — before it happens.

SpiritCuffs learns your repo's standards from its git history (and GitHub PR
history), then reviews your pending diff with a tight rubric (bugs, security,
data-loss — no style noise). It runs locally, in your terminal, in seconds.

```
$ git push
spiritcuffs: reviewing 3 files…
● CRITICAL auth/login.py:42
  SQL query built by string concatenation with user input
  → use a parameterized query
● WARN api/client.py:88
  bare except swallows the timeout error; request hangs forever
  → catch httpx.TimeoutException explicitly
```

## 30-second quickstart

Two one-time commands, then it just runs on every push:

```bash
# 1) authenticate — run once per machine, pick ONE of these:
uvx spiritcuffs setup        # or: pipx install spiritcuffs && spiritcuffs setup
```

The wizard picks a provider (OpenRouter, OpenAI, Anthropic, GitHub Copilot, or
a local endpoint), authenticates, picks a model, and tests the connection.
Scripted/CI variant: `spc setup --non-interactive --provider openrouter
--api-key-env OPENROUTER_API_KEY --skip-ping`. Copilot specifics (device-flow
login, CI flags, plan-gated models) are in [GitHub Copilot](#github-copilot)
below.

```bash
# 2) wire up a repo — run once per repo, same launcher as step 1:
uvx spiritcuffs init         # or: spiritcuffs init (pipx) / spc init
```

`spc init`:

1. writes `.spc.toml` (committed — it's your team's shared rubric),
2. installs a `pre-push` hook (`spc hook --remove` to uninstall; `git push --no-verify` to bypass once),
3. discovers conventions (`CONTRIBUTING.md`, `AGENTS.md`, lint configs),
4. mines your git history (hotspots, bug clusters, test co-change) and GitHub
   PR/review-comment history, then synthesizes `.spc/profile.md` — one LLM call,
5. runs your **first review immediately**.

Every later `git push` triggers `spc review` automatically. You can always
invoke the same launcher interchangeably — `uvx spiritcuffs <cmd>`,
bare `spiritcuffs <cmd>` or `spc <cmd>` if pipx-installed — all of them run
the identical CLI.

| command | what it does |
|---|---|
| `spc setup` | onboarding wizard (provider, key, model, prefs) — once per machine |
| `spc init` | repo wiring + learning + first review — once per repo |
| `spc learn` | re-mine history, re-synthesize `.spc/profile.md` |
| `spc review` | review upstream diff (staged fallback) — what the hook runs |
| `spc history` | recent runs for this repo |
| `spc hook --remove` | uninstall the hook |

### What the LLM sees

Every review sends exactly four things to the model — nothing else leaves
your machine:

1. **Your pending diff** (capped at `max_diff_kb`, oversized files dropped and
   listed in the output).
2. **The repo profile** (`.spc/profile.md`) — the synthesized review standards,
   capped at ~6000 chars.
3. **Convention files** (`CONTRIBUTING.md`, `AGENTS.md`, lint configs — capped
   at ~8000 chars).
4. **Hotspot file paths** — just names of historically high-churn files, so
   they get slightly more scrutiny.

It does **not** see your source code, your `evidence.json` (raw PR data),
your history log, or anything else on disk. Secrets in the diff are regex-
scanned *before* the call; see the privacy section below.

`spc learn` is a separate, one-off call with its own inputs (history
statistics, relationship metadata, and — unless you opt out — bounded historical
excerpts). See [Historical mining](#historical-mining).

### GitHub Copilot

Works with any Copilot plan (Free included). Choosing it runs the official
GitHub device-flow login (open a URL, enter a code) and stores only the
long-lived GitHub OAuth token. CI: `spc setup -y --provider copilot
--copilot-token <github-oauth-token>` (or export `COPILOT_GITHUB_TOKEN`).
GitHub Enterprise is not supported yet.

**Models:** Copilot plan gates model access — the wizard default
`claude-sonnet-4.6` is a broad-coverage GA chat model; override with
`--model`. Token-exchange fallback details (403/404 → direct token use; 401 →
re-auth) are documented in the privacy section below.

## Privacy: what leaves your machine

- The **diff** and, when GitHub mining is enabled, **PR titles + review-comment
  snippets** are sent to the LLM provider configured in `.spc.toml` (`base_url`
  + `model`). Point `base_url` at a local endpoint (Ollama, LM Studio, vLLM)
  and nothing leaves your machine.
- **`spc learn` also sends bounded historical excerpts** — up to 12 changed
  lines each from up to 60 relationship commits (reverts, fixes) — so the model
  can see the *shape* of past failures. Both diff sides are secret-redacted, the
  prompt forbids quoting them, and a leak filter drops any profile line that
  reproduces one. Set `[learn] send_code_excerpts = false` to mine metadata only
  and never read historical code at all. Details: [docs/git-evidence.md](docs/git-evidence.md).
- A **local regex scan** checks the diff for obvious secrets *before* the LLM call.
  Set `block_on_secrets = true` to skip the LLM call entirely when one is suspected.
- **API keys / tokens** live in `~/.spc/config.toml` (mode 0600, outside any
  repo) or in your environment — never in the committed `.spc.toml`, never in
  `.spc/` artifacts, and they are masked in all output.
- **Copilot tokens**: the long-lived OAuth token is exchanged per run for a
  short-lived API token (never persisted). If the exchange is rejected with
  403/404, that long-lived token is used *directly* against the Copilot API
  host as a fallback (a 401 — revoked token — raises instead). Re-run
  `spc setup` to mint a fresh, exchangeable token.
- `.spc/` (profile, evidence) is gitignored; `.spc.toml` is meant to be committed.
- Run history lives in `~/.spc/history/<repo>-<hash>.jsonl` — local only.

## Configuration (`.spc.toml`)

Every knob has a working default:

```toml
[llm]
base_url = "https://openrouter.ai/api/v1"   # any OpenAI-compatible endpoint
model = "anthropic/claude-3.5-haiku"        # any fast/cheap chat model works
api_key_env = "SPC_API_KEY"                 # env var holding the key
api_key = ""                                # optional inline key — DON'T commit
                                            # secrets here; prefer `spc setup`
timeout = 30.0                              # seconds; caps each LLM attempt

[review]
show_nits = false          # nits hidden by default (signal, not noise)
strict = false             # true => exit 1 on a critical finding (blocks push)
max_diff_kb = 200          # diff truncation ceiling; dropped files are listed
cache_diff_hash = true     # reprint cached findings on identical re-review

[secrets]
block_on_secrets = false   # true => skip the LLM call when a secret is suspected

[learn]
learn_max_commits = 2000   # git history depth to mine
profile_max_age_days = 30  # staleness notice threshold (never auto-refresh)
send_code_excerpts = true  # mine bounded, secret-redacted diff excerpts from
                           # relationship commits (reverts, fixes). false =>
                           # commit metadata only, no historical code at all

[github]
enabled = "auto"           # "auto" | true | false — mine merged PRs + review
                           # comments in `spc learn` (token via GITHUB_TOKEN /
                           # GH_TOKEN / gh auth token; public repos work
                           # unauthenticated)
base_url = "https://api.github.com"  # change for GitHub Enterprise
github_max_prs = 200       # merged-PR cap per mining run
github_max_comments = 1000 # inline-comment cap per mining run
github_pages_max = 10      # pagination ceiling per endpoint
```

Keys resolve in order: repo `.spc.toml` `llm.api_key` → user config
`~/.spc/config.toml` (written by `spc setup`) → the env var named by
`api_key_env`. GitHub tokens: user config `github.token` → `GITHUB_TOKEN` →
`GH_TOKEN` → `gh auth token`. Copilot OAuth tokens: repo `.spc.toml`
`copilot.github_oauth_token` → user config `copilot.github_oauth_token` →
`COPILOT_GITHUB_TOKEN`.

Export your key: `export SPC_API_KEY=sk-...` (any OpenAI-compatible provider).

### GitHub mining

If `origin` points at GitHub, `spc learn` also mines **merged PRs and inline
review comments** and distills them into your profile: recurring peer-review
themes plus explicit repo **rules** a diff can be checked against (ordering
requirements, forbidden patterns, required process — e.g. migration before
code). Watermark-incremental: re-runs fetch only PRs newer than the last run.
Rate-limit aware, capped per run, and strictly fail-soft — any GitHub error
downgrades to git-only learning with a one-line notice. Only synthesized
**themes** enter `.spc/profile.md`; raw comments stay in the gitignored
`.spc/evidence.json`.

### Historical mining

Git history is mined into structured evidence: per-commit records, plus
**relationships** — reverts (a fact git asserts), fixes that modify code an
earlier commit touched (a *hypothesis*, never a claim), fixes that touched no
test, and files that repeatedly change together. Profile lines derived from them
are marked `[observed]` or `[hypothesis]` and the model is told to treat a "fix"
keyword as a keyword, not a root cause.

Schema, caps, and the privacy/performance reasoning: [docs/git-evidence.md](docs/git-evidence.md).

### Token safety (GitHub Enterprise)

Your GitHub token is a machine-level credential, but `github.base_url` comes
from the repo-committed `.spc.toml`. SpiritCuffs therefore attaches the
`Authorization: Bearer …` header **only** when the API host is
`api.github.com`, or matches the host of **your own `origin` remote** (your
GitHub Enterprise instance), and only over **https**. A repo whose config
points `base_url` at some other host is mined unauthenticated with a one-line
warning — your token is never sent there. Plain `http://` base URLs never
receive the token either.

## Behavior guarantees

- **Fail-soft**: git hiccups, missing profile, or LLM failure never break your
  push — you get a one-line warning and exit 0. Only `strict = true` + a critical
  finding exits 1 (bypass with `git push --no-verify`).
- **No silent truncation**: oversized diffs are cut per-file, and the prompt lists
  which files were dropped.
- **Cached re-reviews**: pushing the same diff twice skips the LLM call.
- **Never invents findings**: hotspot files get mentioned with slightly higher
  scrutiny, but the rubric forbids inventing issues.

## FAQ / troubleshooting

**No API key configured** — reviews fail soft: a one-line warning, exit 0,
your push goes through. Run `spc setup` (or `export SPC_API_KEY=sk-...`).

**A review blocked my push** — that only happens with `strict = true` plus a
critical finding. Fix it, or bypass once with `git push --no-verify`.

**I disagree with a finding / it's a false positive** — findings are advisory
(except under `strict = true`); ignore them and move on. `spc history` keeps
every run, and v1.1 adds accept/dismiss marking so repeated dismissals can
calibrate future reviews (v2).

**Stale profile notice** — `.spc/profile.md` is older than
`profile_max_age_days` (30). Re-run `spc learn` when convenient; reviews
continue with the stale profile meanwhile. Learning is never triggered inside
the push path.

**CI**: skip the hook entirely — SpiritCuffs is a local dev tool. For scripted
setup use `spc setup --non-interactive --provider <p> --api-key-env <VAR>
--skip-ping`.

**Windows**: the pre-push hook runs via `sh` (Git for Windows ships one);
Python must be on PATH in that shell.

## Development

```bash
uv venv && uv pip install -e . pytest ruff
uv run pytest        # test suite (LLM calls are mocked; no network)
uv run ruff check spc tests
```

MIT licensed. Source: https://github.com/wallycarvalho/spiritcuffs

## Releasing

Run `python3 scripts/release.py patch` (or `minor` / `major`) — it bumps the
version everywhere and rewrites `CHANGELOG.md`. Eyeball the changelog, then
run the git + `uv publish` commands the script prints.
