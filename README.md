# SpiritCuffs (`spc`)

An LLM-powered second opinion on every `git push` — before it happens.

SpiritCuffs learns your repo's standards from its git history, then reviews your
pending diff with a tight rubric (bugs, security, data-loss — no style noise).
It runs locally, in your terminal, in seconds.

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
uvx spiritcuffs init          # or: pipx install spiritcuffs && spc init

# from source:
uv tool install -e .
spc init
```

`spc init` in any git repo:

1. writes `.spc.toml` (committed — it's your team's shared rubric),
2. installs a `pre-push` hook (`spc hook --remove` to uninstall; `git push --no-verify` to bypass once),
3. discovers conventions (`CONTRIBUTING.md`, `AGENTS.md`, lint configs),
4. mines your git history (hotspots, bug clusters, test co-change) and synthesizes
   `.spc/profile.md` — one LLM call,
5. runs your **first review immediately**.

Then every `git push` triggers `spc review` automatically. Plain commands:

| command | what it does |
|---|---|
| `spc init` | setup + learning + first review |
| `spc learn` | re-mine history, re-synthesize `.spc/profile.md` |
| `spc review` | review upstream diff (staged fallback) — what the hook runs |
| `spc history` | recent runs for this repo |
| `spc hook --remove` | uninstall the hook |

## Privacy: what leaves your machine

- The **diff** and (optionally, in a later version) **PR history** are sent to the
  LLM provider configured in `.spc.toml` (`base_url` + `model`). Point `base_url`
  at a local endpoint (Ollama, LM Studio, vLLM) and nothing leaves your machine.
- A **local regex scan** checks the diff for obvious secrets *before* the LLM call.
  Set `block_on_secrets = true` to skip the LLM call entirely when one is suspected.
- `.spc/` (profile, evidence) is gitignored; `.spc.toml` is meant to be committed.
- Run history lives in `~/.spc/history/<repo>-<hash>.jsonl` — local only.

## Configuration (`.spc.toml`)

Every knob has a working default:

```toml
[llm]
base_url = "https://openrouter.ai/api/v1"   # any OpenAI-compatible endpoint
model = "anthropic/claude-3.5-haiku"        # fast/cheap default; your choice
api_key_env = "SPC_API_KEY"                 # env var holding the key

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
enabled = "auto"           # reserved for step 2 (GitHub PR/comment mining)
```

Export your key: `export SPC_API_KEY=sk-...` (any OpenAI-compatible provider).

## Behavior guarantees

- **Fail-soft**: git hiccups, missing profile, or LLM failure never break your
  push — you get a one-line warning and exit 0. Only `strict = true` + a critical
  finding exits 1 (bypass with `git push --no-verify`).
- **No silent truncation**: oversized diffs are cut per-file, and the prompt lists
  which files were dropped.
- **Cached re-reviews**: pushing the same diff twice skips the LLM call.
- **Never invents findings**: hotspot files get mentioned with slightly higher
  scrutiny, but the rubric forbids inventing issues.

## Roadmap

- **v1 (this)**: local harness — git mining + LLM profile + push review
- **v1.1**: npm wrapper (`spiritcuffs`) for `npx` users
- **v1.5**: feedback marking in `spc history`; doc generation
- **v2**: calibration engine learning from your accept/dismiss feedback
- **step 2 (next)**: GitHub PR/review-comment mining → richer profiles

## Development

```bash
uv venv && uv pip install -e . pytest ruff
uv run pytest        # test suite (LLM calls are mocked; no network)
uv run ruff check spc tests
```

MIT licensed.
