# SpiritCuffs — Step 2 Report (GitHub PR + review-comment mining)

Step 2 layers GitHub evidence onto the step-1 git-only harness. `spc learn`
now mines merged PRs and inline review comments from the repo's GitHub
remote, clusters recurring review themes with one LLM call, and feeds those
themes into the profile synthesis — all fail-soft, all optional.

## What works

| Command | Behavior |
|---|---|
| `spc learn` | mines git history (unchanged) **and** GitHub PR/comment history when available, then synthesizes the profile |
| `spc review` | unchanged; profiles may now mention peer-review themes |
| `spc init` / `spc hook` / `spc history` | unchanged |

## How GitHub mining plugs in

- `spc/githubapi.py` — thin read-only REST client (httpx, no new deps):
  - `parse_github_remote(url)` — https + ssh (`git@github.com:owner/repo.git`,
    `ssh://git@github.com/owner/repo`) remote forms; non-GitHub → skip.
  - `resolve_token()` — `GITHUB_TOKEN` → `GH_TOKEN` → `gh auth token` (only if
    the `gh` binary exists, 5 s timeout, failures ignored). No token is legal:
    public repos are mined unauthenticated in `auto` mode.
  - `GithubClient` — capped pagination (`github_max_prs=200`,
    `github_max_comments=1000`, `github_pages_max=10`, hard ceiling of 60 API
    calls per run), rate-limit tracking via `X-RateLimit-Remaining` (0 →
    graceful stop, keeping everything already fetched — `RateLimitedError` is
    caught *inside* the fetch loop so partial evidence is not discarded),
    one-shot backoff on 403/429, `github.base_url` honored (GitHub Enterprise).
- `spc/githubhistory.py` — mining + synthesis:
  - `mine_github(...)` → `GithubEvidence` (PR records + comment snippets +
    stats). Watermark-incremental: the highest seen PR number is stored in
    `.spc/evidence.json`; re-runs fetch only newer PRs and merge into the old
    evidence (same repo only).
  - `cluster_themes(...)` — ONE cheap LLM pass over capped, truncated
    evidence (≤100 PR titles, ≤300 comment snippets ≤200 chars, ≤14 000 chars
    of prompt) producing a short recurring-themes markdown section.
- `spc/profile.py` — evidence store upgraded to `{"schema": 2, "sources":
  {"git": …, "github": …}}`. Step-1 flat files are transparently upgraded on
  load. `build_profile(extra_evidence_md=…)` — prepared in step 1 — receives
  the synthesized themes as its extra evidence section.
- `spc/learn.py` — orchestrates: git mine → GitHub mine → cluster → profile.
  Every GitHub path degrades to git-only with a one-line notice; `learn`
  never raises.

## Decision matrix (`[github] enabled`)

| Value | Behavior |
|---|---|
| `"auto"` (default) | mine if a token is found, else try unauthenticated; any failure → git-only |
| `true` | mine (still fail-soft on errors) |
| `false` | never touch the network; existing cached evidence is still summarized locally |

## Privacy (locked requirement)

Only the **synthesized themes** (LLM output) enter `.spc/profile.md`. Raw PR
titles and comment snippets stay in the gitignored `.spc/evidence.json`. The
clustering prompt itself instructs the model to never quote comments
verbatim, name authors, or include code/secrets. Covered by tests: a raw
comment string is asserted absent from the profile prompt.

## Test coverage (new: 38 tests in `tests/test_github.py`, suite total 130)

- remote parsing: 6 github forms + 6 non-github rejections
- token resolution order incl. fake `gh` binary probe; no-token unauth path
- pagination stops at `pages_max`; caps on PRs and comments (trimmed budget)
- rate-limit stop: first page kept, walk ends gracefully, further calls raise
- watermark increment: second run fetches only newer PRs, merges old+new,
  `stats.prs_fetched == 1`; no-new-PRs run returns None
- evidence merge: git + github blocks coexist in schema-2 evidence.json
- privacy: raw comment text never reaches the profile prompt; cluster output
  does
- fail-soft: 401/404/network errors → `mine_github` returns None, `run_learn`
  still completes git-only and returns True
- `enabled = false` never issues a single HTTP request (forbidden mock)
- clustering prompt: capped contents (PR #149 and comment #350 absent when
  capped), bounded size, defensive parse (LLM exception → None)

## Dogfood results

- This repo has **no GitHub remote**: `spc learn` prints
  `origin is not a github remote — skipping GitHub mining.` and completes
  git-only (9 commits, 20 hotspots, 1 bug class → `.spc/profile.md` via mock
  LLM; evidence.json is schema 2 with a git block only). ✅
- Stale-token live scenario: this machine's `gh` binary holds a revoked
  token; with a fabricated `origin` pointing at `github.com/acme/widget`
  (nonexistent repo), `mine_github` failed soft → None, learn unaffected. ✅
- Unauthenticated live probe (gh + token envs disabled): mined 8 merged PRs
  from the real public repo `psf/requests` in 6 API calls, capped correctly
  (`stopped_reason: cap_prs`). Those PRs carry no inline review comments, so
  the comments list is empty — the endpoint was still exercised per-PR. ✅

## Deviations / notes

- `RateLimitedError` (subclass of `GithubApiError`) added so rate-limit
  exhaustion keeps partial evidence instead of discarding the run — the spec
  said "stop fetching gracefully (keep what was fetched)". Caught inside the
  fetch loop; `stats.stopped_reason = "rate_limit"` records it.
- `spc/learn` prints a deterministic cached-evidence summary (top commented
  paths) instead of re-clustering when GitHub mining is skipped but old
  evidence exists — no extra LLM cost on re-runs.
- Comment bodies are truncated to 400 chars in evidence, 200 in the
  clustering prompt; titles to 300 chars. Enforced at both record and prompt
  levels.
- GitHub general PR comments (`/issues/{n}/comments`) are **not** fetched in
  this step — inline review comments (`/pulls/{n}/comments`) carry the
  higher-signal review feedback. The client makes adding the second endpoint
  a ~10-line change if wanted later.

## Step-2 integration points honored (from step 1)

- `save_evidence`/`save_github_evidence` write tagged blocks into one
  `.spc/evidence.json` (`sources.git`, `sources.github`) — a third source
  would add one more key.
- `build_profile(evidence, config, extra_evidence_md=…)` — the step-1
  parameter is now fed by `cluster_themes` output.
- `github.*` config keys parsed since step 1 are now consumed; defaults
  unchanged (max_prs=200, max_comments=1000, pages_max=10,
  base_url=https://api.github.com).
