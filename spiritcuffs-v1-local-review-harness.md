# SpiritCuffs — v1: Local Review Harness

A lightweight local tool that reviews your code **before you push**. Set it up once per repo; every `git push` gets a fast, LLM-powered second opinion in your terminal.

**Working name:** SpiritCuffs (`spc`). Rename freely.

**North star (later):** the trust-ladder review agent — a reviewer that learns a team's standards and earns approval autonomy with measurable metrics. V1 deliberately ships none of that; it just builds the daily-use foundation and quietly logs the data that future learning will need.

---

## 1. What v1 does

1. `spc init` — one-time setup per repo:
   - Installs a **git pre-push hook** (opt-out anytime: `git push --no-verify`)
   - Detects project type (`pyproject.toml`, `package.json`, `go.mod`, etc.)
   - Creates `.spc.toml` with sensible defaults
   - Auto-discovers repo conventions: `CONTRIBUTING.md`, `AGENTS.md`, lint configs → becomes part of the review prompt
   - **Runs the learning phase** (below) to build the repo profile
   - **Ends with the first review** — the user sees the tool's value in the same minute they installed it. The first pass is the moment that earns daily use.
2. **Learning phase — evidence mining at setup (local git + GitHub PR history + LLM synthesis):**
   This is the product differentiator. The first profile is what earns the user's trust, so it mines every signal a senior reviewer would actually use — not just churn counts. All raw evidence lives in `.spc/evidence.json`; the synthesized narrative lives in `.spc/profile.md`.

   **Source A — local git history (always; offline; no network):**
   - **Hotspots**: most frequently changed files/dirs → higher-scrutiny zones
   - **Past bug classes**: `fix` / `revert` / `hotfix` clusters by file
   - Test layout and source↔test co-change ratio (informs "missing tests" warnings)
   - Languages present and rough proportions

   **Source B — GitHub PR history (read-only; when `gh` is authenticated or a token env var is set):**
   - **PR review comments**: inline review comments (file:line + reviewer) — literally "what a senior reviewer says about code." Clustered into recurring themes: *"reviewers on this repo repeatedly flag missing input validation, unbounded queries, swallowed exceptions."*
   - **Review outcomes**: which change categories draw "changes requested" vs. approval → this repo's real risk categories
   - **Comment hotspots**: files that attract the most review comments — a sharper hotspot signal than churn frequency alone
   - **PR shape & rework**: size, files touched, multi-round review, later reverted
   - **Reviewer patterns**: who reviews what, and with what emphasis
   - Caps keep it bounded — all overridable in `.spc.toml`: `github_max_prs` (**default 200** most recent merged), `github_max_comments` (**default 1000** inline review comments fetched total), `github_pages_max` (**default 10** API pages). Mining is **watermark-incremental** (stores the last PR number seen) so `spc learn` re-runs are cheap and don't re-hit the API. Raw evidence is cached locally and re-summarizable without network.

   **Synthesis — 1–2 LLM calls assemble `.spc/profile.md`** (human-readable, editable): a narrative of this repo's standards, hotspots, recurring review themes, and risk areas — *"This repo: `auth/` and `api/handlers/` are comment-heavy hotspots; reviewers consistently ask for input validation and bounded queries. Tests mirror `src/` and are expected in the same PR."* If comment volume is large, a first call clusters comments into themes and a second synthesizes the profile. **Only synthesized themes enter the profile — never raw comment text, file contents, or secrets.**
   - **`spc init` ends by running the first review immediately** — the user sees value in the same minute.
   - Refresh: manual `spc learn` only. **Never inside the push path.**
   - No server, no GitHub App — just local `gh`/token reads. If `gh` is missing/unauthenticated and no token is set, it degrades gracefully to git-only mining with a one-line note (still works on any clone).
3. On every push, the hook runs `spc review`:
   - Computes the diff about to be pushed (`@{upstream}...HEAD`; falls back to staged changes if no upstream)
   - If the profile is stale (older than `profile_max_age_days`, default 30), print a **one-line notice** ("profile is 34 days old — run `spc learn`") and continue with the stale profile. Refresh is never automatic in the push path.
   - **Diff-hash cache**: if the diff hash matches the last review in the JSONL log, skip the LLM call and reprint cached findings. Retried pushes are common — this makes the tool feel instant.
   - **Local secrets scan**: cheap regex scan for obvious secret patterns on the diff *before* the LLM call. On a hit: warn prominently. If `block_on_secrets = true` in config, skip the LLM call entirely (diff never leaves the machine).
   - Sends diff + **repo profile** + conventions to the LLM with a tight rubric: bugs, security, obvious perf issues, missing tests. **No style nits by default**
   - Prints findings grouped by severity (`critical` / `warn` / `nit`) with file:line refs
   - Exit code stays **0 unless `strict` is set** in config and a `critical` is found — v1 advises, it doesn't gate
4. Every run appends a **JSONL log** (`~/.spc/history/<repo-slug>.jsonl`): diff hash, findings, severities. Repo slug = repo name + 8-char path-hash suffix to avoid collisions across same-named repos. Zero UI, zero complexity — this is the seed data for the future learning loop (v2).

## 2. Non-goals for v1

- No GitHub App, no server, no PR comments *written back*, no approvals (we **read** PR history; we never post to GitHub)
- No learning/calibration loop from the user's own feedback (v2+)
- No blocking gates (except explicit `strict` opt-in)
- No config beyond one TOML file
- **No npm wrapper** (deferred to v1.1 — *shipped early: included in the v1 release prep, see step 3 below*)
- **No doc generation** (v1.5)
- **No agent framework / orchestration library** (see §7)

## 3. Distribution: Python core

**Core:** pure Python 3.11+ package on PyPI. Install via `pipx install spiritcuffs` or run ad hoc with `uvx spiritcuffs init`. Deps kept minimal: `httpx`, `typer`, `rich`, `tomli`/`tomli-w`.

**LLM calls:** plain `httpx` against any **OpenAI-compatible endpoint**. Config: `base_url` + `model` + API key from env var. Covers OpenAI, OpenRouter, Anthropic-compatible endpoints, Ollama, LM Studio, vLLM. No litellm — lighter install, fewer breakage points.

**GitHub Copilot provider:** powered by the official GitHub partnership device flow (as integrated by OpenCode). One-time OAuth Device Flow login (`github.com/login/device/code`, client `Iv1.b507a08c87ecfe98` — the Copilot CLI GitHub App; the exchange endpoint only accepts `ghu_` tokens minted by GitHub Apps, custom-OAuth-App `gho_` tokens get 404) → long-lived GitHub OAuth token stored in the user config (mode 0600); soft entitlement check (`api.github.com/copilot_internal/user`, `Authorization: Bearer`, keeps `X-GitHub-Api-Version: 2022-11-28` — only an explicit chat-disabled answer blocks); per-run exchange (GET `api.github.com/copilot_internal/v2/token`, `Authorization: token <ghu_>` — POST and `Bearer` are rejected) for a short-lived (~30 min) API token that is **never persisted**. Chat calls go to `endpoints.api` (fallback `api.githubcopilot.com`) with required headers `Openai-Intent: conversation-panel`, `x-initiator: user`, `Copilot-Integration-Id: vscode-chat`, `Editor-Version: vscode/1.98.0`, `Editor-Plugin-Version: copilot-chat/0.26.7` — and must NOT carry `X-GitHub-Api-Version` (the chat endpoint 400s on it). If the exchange is rejected with 403/404, the raw long-lived OAuth token is used directly against the Copilot API host (degraded mode; the setup ping prints a notice); a 401 (revoked token) raises. Works with any Copilot plan (Free included); plan gates model access, and model availability is also integrator-gated (a curated shortlist is an offer, not a contract). **GitHub Enterprise not supported yet.**

**npm wrapper (`spiritcuffs` on npm):** shipped with the v1 release prep (originally deferred). A thin ~50-line Node bin script that detects `uvx`/`pipx`, prints a one-line install hint if missing, and spawns `uvx spiritcuffs@latest <args>` forwarding exit codes.

**Language support:** the review is diff-based, so any language works out of the box. Language-awareness stays cheap and optional: file-extension → language tag for the prompt, plus per-language "check for" hints (e.g., `.ts` → type narrowing, `.py` → mutable defaults). No AST parsing in v1.

## 4. Package layout

```
spc/
  cli.py          # typer entrypoints: init, learn, review, hook, history
  diff.py         # git plumbing: upstream diff, staged fallback, diff hashing
  learn.py        # orchestrates evidence mining → synthesis → profile
  githistory.py   # Source A: local git mining (hotspots, bug classes, tests)
  github.py       # Source B: PR/review-comment mining via gh CLI or token
  evidence.py     # evidence.json schema, caching, incremental watermark
  profile.py      # profile synthesis (LLM), build/refresh, staleness, load
  review.py       # prompt assembly + LLM call + response parsing
  secrets.py      # local regex scan for secret patterns on the diff
  llm.py          # httpx OpenAI-compatible client (base_url, model, env key)
  conventions.py  # discover CONTRIBUTING/AGENTS/lint configs
  languages.py    # extension → language + per-language hints
  config.py       # .spc.toml load/write, defaults
  hook.py         # install/remove git pre-push hook (idempotent)
  log.py          # JSONL history append (path-hash repo slug) + `spc history`
```

## 5. Review prompt rubric (v1)

- **Critical:** bugs, logic errors, security issues (injection, secrets, auth gaps), data-loss risks
- **Warn:** missing error handling, untested risky paths, perf footguns
- **Nit:** omitted by default (config: `show_nits = true` to include)
- Output format: strict JSON array of `{severity, file, line, message, suggestion}` — parsed, never shown raw
- Hard cap: ~10 findings per run, highest severity first — protects the "signal, not noise" promise from day one
- **Truncation:** when `max_diff_kb` (default 200) truncates the diff, the prompt lists *which files* were dropped, not just "truncated" — no silent blind spots

## 6. Roadmap

1. **v1 (now):** local harness with deep setup-time learning (git + GitHub PR/review-comment mining → LLM-synthesized profile) — usable in any repo, any language
2. **v1.1:** npm wrapper improvements; keychain/keyring storage for API keys; `spc history` accept/dismiss marking → real feedback data accumulates
3. **v1.5:** **generate default docs** (AGENTS.md, CONTRIBUTING.md skeleton) when a repo is missing them, using the same LLM plumbing
4. **v2:** calibration engine learns per-repo category thresholds from that feedback history
5. **v3:** shadow-mode PR prediction (the original vision) — by then the data collector is mostly written
6. **Release prep (before/at v1 public launch):** interactive onboarding wizard (`spc setup`-style: select LLM provider, authenticate, choose model — opencode/claude-code-style CLI), polished README, and public-release prep (PyPI publishing, npm wrapper, docs)

Note the learning distinction: v1 *learns the repo* (git + PR history mining + LLM synthesis at setup); v2 *learns from you* (feedback-driven calibration over time). They stack.

## 7. Do we need an agent framework (LangChain, etc.)?

**No. Do not add one.** Reasoning:

**What we actually use an LLM for is narrow and fixed:**
1. *Synthesis* (learn): feed structured evidence (git stats + clustered review comments) → get a prose profile. One or two calls, no tools, no loops.
2. *Review* (push): feed diff + profile + rubric → get strict JSON. One call, no tools, no loops.

Neither is an "agent" in the framework sense — there's no planning loop, no tool selection, no memory graph, no multi-agent handoff, no retrieval index. What frameworks sell is orchestration for exactly those things. We have a single-shot prompt→response with a JSON contract. **`httpx.post()` is the whole integration.**

**Cost of adopting LangChain/LangGraph/CrewAI/etc.:**
- **Dependency weight**: LangChain pulls a large transitive tree (pydantic, many adapters, community packages). Our whole promise is `uvx spiritcuffs` running instantly on a fresh machine. Every heavy dep is install time and a breakage vector.
- **Version churn**: these libraries move fast and break APIs between minors. A tool that runs on every `git push` for every teammate must be boring and stable for years.
- **Debug opacity**: when a review is wrong, we want to read the exact prompt and the exact response. Framework indirection (chains, runnables, callbacks) makes the one thing we care about — prompt/response fidelity — harder, not easier.
- **Provider lock-in disguised as agnosticism**: we already get provider-agnosticism more simply via the OpenAI-compatible `base_url` convention.

**What we build instead (already in scope):** a ~100-line `llm.py` with `complete(messages, model, base_url, api_key) -> str`, retry-with-backoff, timeout, and a fail-soft path. Plus defensive JSON parsing in `review.py`. That is genuinely all the "framework" this product needs.

**When a framework *might* earn its place (honest counterpoint, for v2+):** if the calibration engine grows into multi-step reasoning over feedback history (retrieve relevant past findings → reason about thresholds → tool-using evaluation), a small orchestration layer could help. If that day comes, prefer the **thinnest** option (e.g., LiteLLM for routing, or LangGraph only for a genuine state machine) and adopt it as a **separate, optional extra**, never a core dependency. Even then, our own state is simple enough that hand-rolled control flow will likely still win.

**Rule of thumb that settles it:** frameworks pay for themselves when you have *dynamic control flow* (loop-until-done, choose-a-tool, branch-on-result). We have *static control flow* (always: mine → summarize; always: diff → review). Static flow = plain functions.

## 8. Prompt for your coding agent

Copy everything inside the fence:

```text
You are helping me build "SpiritCuffs" (spc), a lightweight local tool
that reviews code before git push. Python 3.11+ core, distributed on PyPI.
NO server, NO GitHub App, NO feedback-calibration loop, NO npm wrapper in
this version — but it DOES learn a repo profile at setup from local git
history AND GitHub PR/review-comment history, synthesized by LLM.

## Behavior
- `spc init`: detects project type (pyproject.toml / package.json / go.mod
  / Cargo.toml), writes .spc.toml with defaults, installs a git pre-push
  hook (idempotent install; `spc hook --remove` to uninstall), discovers
  repo conventions (CONTRIBUTING.md, AGENTS.md, .editorconfig, eslint/ruff/
  flake8 configs), then runs the learning phase, then runs the first review
  immediately (the user should see value in the same minute they installed).

- `spc learn` (learning phase, also run by init; git + GitHub PR history +
  LLM synthesis). This is the product differentiator — the first profile
  must be genuinely insightful, not a churn table.
  - Source A — local git (always, offline):
    * hotspots: top 20 most-changed files/dirs (git log --name-only
      frequency), excluding vendored/lockfile/generated paths
    * past bug classes: frequency and file clusters of fix/revert/hotfix
      keywords in commit subjects
    * test layout: where tests live, source<->test co-change ratio
    * languages present with rough proportions (by file extension)
  - Source B — GitHub PR/review history (read-only; only when `gh` is
    authenticated or a GitHub token env var is set):
    * fetch recent merged PRs (cap: github_max_prs, default 200) via
      `gh` CLI (preferred) or the GitHub REST API with a token
    * for each: title, body, size, files touched, review decisions
      (approved / changes-requested), and INLINE REVIEW COMMENTS
      (file:line + reviewer + comment body)
    * derive: comment hotspots (files attracting the most review
      comments), recurring review themes (what reviewers repeatedly ask
      for), change categories that draw "changes requested", reviewer
      patterns, rework/revert signals
    * watermark-incremental: store the last-seen PR number in evidence
      state so re-runs only fetch new PRs (cheap, API-friendly)
    * configurable caps (all in .spc.toml, all with defaults):
      github_max_prs=200, github_max_comments=1000, github_pages_max=10
  - Persist ALL raw evidence to `.spc/evidence.json` (local cache;
    re-summarizable without network).
  - Synthesis: ONE or TWO LLM calls turn evidence into `.spc/profile.md`
    (human-readable, editable). If comment volume is large, first call
    clusters comments into recurring themes, second synthesizes the
    profile narrative. The profile is a narrative of THIS repo's standards,
    hotspots, recurring review themes, and risk areas. ONLY synthesized
    themes go into the profile — never raw comment text, file contents, or
    secrets.
  - If Source B is unavailable (no gh / no token / unauthenticated): print
    a one-line note and continue with git-only evidence. Still works on any
    clone.
  - If the LLM call fails: print a one-line warning and exit 0 — no
    fallback profile; the user re-runs `spc learn` later.
  - Staleness: review prints a one-line notice when the profile is older
    than profile_max_age_days (default 30) and continues with the stale
    profile. Refresh is manual only (`spc learn`) — never inside the push
    path.

- `spc review` (also invoked by the hook):
  - Diff = `git diff @{upstream}...HEAD`; if no upstream, fall back to
    `git diff --cached`; if both empty, exit 0 silently. Handle detached
    HEAD gracefully.
  - **Diff-hash cache**: if the diff hash matches the last review in the
    JSONL log, reprint cached findings and skip the LLM call.
  - **Secrets scan**: regex scan the diff for obvious secret patterns
    (AWS keys, private keys, generic token= assignments) BEFORE the LLM
    call. On hit: warn prominently. If `block_on_secrets = true` in config,
    skip the LLM call (diff never leaves the machine).
  - Assemble prompt: diff + repo profile + convention file contents
    (truncated sensibly) + rubric. Rubric: critical = bugs/logic/security/
    secrets/data-loss; warn = missing error handling, untested risky paths,
    perf footguns. NO style nits unless show_nits=true in config. Files in
    the hotspot list may be mentioned with slightly higher scrutiny, but
    never invent findings just because a file is a hotspot.
  - LLM must return strict JSON: [{severity, file, line, message,
    suggestion}]. Parse defensively; cap at 10 findings, highest severity
    first. LLM via plain httpx against an OpenAI-compatible endpoint
    (config: base_url, model, api_key_env). No litellm.
  - Truncation: truncate diffs over max_diff_kb (default 200); the prompt
    must list which files were dropped, not just "truncated".
  - Print findings with rich: severity color, file:line, message,
    suggestion. Exit 0 always, EXCEPT when strict=true in config AND a
    critical finding exists → exit 1 (blocks push; user can bypass with
    git push --no-verify).
  - Append a JSONL record to ~/.spc/history/<name>-<pathhash8>.jsonl:
    timestamp, diff hash (sha256 of diff), files touched, findings, model
    used. Path-hash suffix prevents collisions between same-named repos.

- `spc history`: pretty-print recent runs from the JSONL log.
- `spc hook --remove`: uninstall the hook cleanly.
- Config defaults: `.spc.toml` is meant to be committed (shared rubric);
  `.spc/` working artifacts should be gitignored by init.

## Layout
spc/
  cli.py          # typer entrypoints
  diff.py         # git plumbing (subprocess git; handle errors gracefully)
  learn.py        # orchestrates mining → synthesis → profile
  githistory.py   # Source A: local git mining (hotspots, bug classes, tests)
  github.py       # Source B: PR + inline review-comment mining (gh or token)
  evidence.py     # evidence.json schema, local cache, incremental watermark
  profile.py      # profile synthesis (LLM), build/refresh, staleness, load
  review.py       # prompt assembly, LLM call, JSON parsing
  secrets.py      # local regex scan for secret patterns
  llm.py          # httpx OpenAI-compatible client (single-shot, retry, timeout)
  conventions.py  # convention file discovery
  languages.py    # file extension → language + short per-language hints
  config.py       # .spc.toml load/write with defaults
  hook.py         # pre-push hook install/remove (idempotent, marker comment)
  log.py          # JSONL history with collision-safe repo slug

## Rules
- Deps limited to: typer, rich, httpx, tomli-w. NO agent framework
  (no langchain/langgraph/crewai/etc.). LLM integration is a single-shot
  httpx call. Ask before adding any other dependency.
- Type hints everywhere, docstrings on public functions, ruff clean.
- pytest tests for: diff command fallbacks (including detached HEAD),
  githistory.py hotspot/bug-class counting against a fixture repo, github.py
  PR/comment parsing against a recorded fixture (no live API in tests),
  evidence watermark incremental logic, profile staleness logic, JSON
  parsing (including malformed LLM output), secrets scan patterns, hook
  install/remove idempotency, config defaults, diff-hash cache behavior.
- All git, GitHub, and LLM calls must fail soft: on error, print a one-line
  warning and exit 0 — never break the user's push. Profile refresh failures
  must degrade to the stale profile, never to no profile. `spc learn` LLM
  failure prints a one-line warning and exits 0 (no fallback profile — user
  re-runs later). Missing/unauthenticated `gh` degrades learn to git-only.
- Keep every run under ~15 seconds for a typical diff; truncate huge diffs
  (config: max_diff_kb, default 200) and note the dropped files in the
  prompt.
- `spc learn` on a big repo may take longer (GitHub fetch + LLM synthesis),
  but only runs at init or manual invocation — never inside the push path.
  Show progress (rich) since first-run mining is the trust moment.
- Never send raw PR comment text, file contents, or secrets into the profile
  or the review prompt beyond what the rubric needs; profile contains only
  synthesized themes.
- Windows: hooks run via sh in Git for Windows; add a test note, not
  silence.

## Deliverables this session
1. Working spc init / learn / review / hook / history end to end
2. .spc/profile.md generated against a real repo (test on this repo itself
   — dogfooding), including GitHub PR/review-comment mining when `gh` is
   available; git-only path verified as fallback
3. README with a 30-second quickstart, including a plain-language statement
   that the diff and (optionally) PR history are sent to the configured
   provider
4. pytest suite green
```

## 9. Open decisions

- **Default model** — a fast/cheap model for v1 (runs on every push); configurable on day one via `base_url` + `model` in `.spc.toml`. **Resolved:** with httpx, config is just `base_url` + `model`.
- **npm scope** — **resolved: shipped with v1 release prep**; wrapper is `spiritcuffs` (unscoped) at version `0.1.0`, matching the Python package.
- **Strict default** — **resolved: `strict = false`.** Blocking pushes out of the box is how tools get uninstalled.
- **Secrets scan default** — **resolved: warn + `block_on_secrets` config option (default false).**
- **Config/artifact split** — **resolved: `.spc.toml` committed, `.spc/` gitignored.**
- **GitHub access** — **resolved: `gh` CLI preferred (uses existing auth), GitHub token env var (`GITHUB_TOKEN` / `SPC_GITHUB_TOKEN`) as alternative. Read-only scopes only (`repo` read for private, public needs none). Never write.** Detected automatically; degrades to git-only learning when unavailable.
- **Learning depth** — **resolved: v1 ships PR/review-comment mining from day one** (not deferred). The first profile is the product differentiator; git-log-only is not enough.
- **Agent framework** — **resolved: none** (see §7). Single-shot httpx.
- **Mining caps** — **resolved: ship with defaults, all user-overridable in `.spc.toml`.** `github_max_prs = 200`, `github_max_comments = 1000`, `github_pages_max = 10`, `learn_max_commits = 2000`, `max_diff_kb = 200`, `profile_max_age_days = 30`. No hard-coded ceilings — a monorepo can raise them, a small repo can lower them to cut first-run time.

## 10. Config reference (`.spc.toml`)

Every knob has a working default; users override any of them at will. Committed to the repo so a team shares one rubric.

```toml
[llm]
base_url = "https://openrouter.ai/api/v1"   # any OpenAI-compatible endpoint
model = "anthropic/claude-3.5-haiku"        # fast/cheap default; user's choice
api_key_env = "SPC_API_KEY"                # env var holding the key

[review]
show_nits = false          # nits hidden by default (signal, not noise)
strict = false             # true => exit 1 on a critical finding (blocks push)
max_diff_kb = 200          # diff truncation ceiling; dropped files listed
cache_diff_hash = true     # reprint cached findings on identical re-review

[secrets]
block_on_secrets = false   # true => skip the LLM call when a secret is suspected

[learn]
learn_max_commits = 2000   # git history depth to mine
profile_max_age_days = 30  # staleness notice threshold (never auto-refresh)

[github]
enabled = "auto"           # auto | true | false (auto = use if gh/token present)
github_max_prs = 200       # most recent merged PRs to mine
github_max_comments = 1000 # total inline review comments fetched
github_pages_max = 10      # API pagination cap
```
