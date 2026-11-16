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

```bash
# run ad hoc (no install):
uvx spiritcuffs setup        # or: pipx install spiritcuffs && spc setup
# or via npx (installs nothing but needs uv or pipx underneath):
npx spiritcuffs setup
```

`spc setup` is a one-time wizard: pick a provider (OpenRouter, OpenAI,
Anthropic, GitHub Copilot, or a local endpoint), authenticate (paste your API
key — stored in `~/.spc/config.toml`, mode 600 — or log in with your Copilot
subscription via the GitHub device flow), pick a model, and it tests the
connection. Scripted/CI variant: `spc setup --non-interactive --provider
openrouter --api-key-env OPENROUTER_API_KEY --skip-ping`.

**GitHub Copilot** works with any Copilot plan (Free included): choosing it
runs the official GitHub device-flow login (open a URL, enter a code), checks
your Copilot entitlement, and stores only the long-lived GitHub OAuth token.
Each review exchanges it for a short-lived API token that is never written to
disk. CI: `spc setup -y --provider copilot --copilot-token <github-oauth-token>`
(or export `COPILOT_GITHUB_TOKEN`). GitHub Enterprise is not supported yet.
Copilot plan gates model access — the wizard default `claude-sonnet-4.6` is
a broad-coverage GA chat model; override with `--model`. On plans where the
token exchange is rejected (403/404 — e.g. older tokens), SpiritCuffs uses
your long-lived OAuth token directly against the Copilot API host instead;
a 401 (revoked token) raises and asks you to re-run `spc setup`.

Then, inside any git repo:

```bash
uvx spiritcuffs init         # writes .spc.toml + hook + profile + first review
```

`spc init`:

1. writes `.spc.toml` (committed — it's your team's shared rubric),
2. installs a `pre-push` hook (`spc hook --remove` to uninstall; `git push --no-verify` to bypass once),
3. discovers conventions (`CONTRIBUTING.md`, `AGENTS.md`, lint configs),
4. mines your git history (hotspots, bug clusters, test co-change) and GitHub
   PR/review-comment history, then synthesizes `.spc/profile.md` — one LLM call,
5. runs your **first review immediately**.

Every later `git push` triggers `spc review` automatically.

| command | what it does |
|---|---|
| `spc setup` | onboarding wizard (provider, key, model, prefs) |
| `spc init` | repo wiring + learning + first review |
| `spc learn` | re-mine history, re-synthesize `.spc/profile.md` |
| `spc review` | review upstream diff (staged fallback) — what the hook runs |
| `spc history` | recent runs for this repo |
| `spc hook --remove` | uninstall the hook |

## Privacy: what leaves your machine

- The **diff** and, when GitHub mining is enabled, **PR titles + review-comment
  snippets** are sent to the LLM provider configured in `.spc.toml` (`base_url`
  + `model`). Point `base_url` at a local endpoint (Ollama, LM Studio, vLLM)
  and nothing leaves your machine.
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
model = "anthropic/claude-3.5-haiku"        # fast/cheap default; your choice
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

### GitHub mining (step 2)

If `origin` points at GitHub, `spc learn` also mines **merged PRs and inline
review comments** and distills them into your profile: recurring peer-review
themes plus explicit repo **rules** a diff can be checked against (ordering
requirements, forbidden patterns, required process — e.g. migration before
code). Watermark-incremental: re-runs fetch only PRs newer than the last run.
Rate-limit aware, capped per run, and strictly fail-soft — any GitHub error
downgrades to git-only learning with a one-line notice. Only synthesized
**themes** enter `.spc/profile.md`; raw comments stay in the gitignored
`.spc/evidence.json`.

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

**Stale profile notice** — `.spc/profile.md` is older than
`profile_max_age_days` (30). Re-run `spc learn` when convenient; reviews
continue with the stale profile meanwhile. Learning is never triggered inside
the push path.

**CI**: skip the hook entirely — SpiritCuffs is a local dev tool. For scripted
setup use `spc setup --non-interactive --provider <p> --api-key-env <VAR>
--skip-ping`.

**Windows**: the pre-push hook runs via `sh` (Git for Windows ships one);
Python must be on PATH in that shell.

## Roadmap

- **v1 (this)**: local harness — git + GitHub PR/comment mining, LLM profile,
  push review, setup wizard, npm wrapper
- **v1.1**: feedback marking in `spc history`; keychain/keyring storage for keys
- **v2**: calibration engine learning from your accept/dismiss feedback

## Development

```bash
uv venv && uv pip install -e . pytest ruff
uv run pytest        # test suite (LLM calls are mocked; no network)
uv run ruff check spc tests
```

MIT licensed. Source: https://github.com/wallycarvalho/spiritcuffs
