# Git evidence schema

`spc learn` mines the repository's history into `.spc/evidence.json`. This
document describes what is stored, what the relationship kinds mean, and the
privacy/performance tradeoffs behind the caps.

The file is a gitignored local artifact — it is the archival record the profile
is synthesized from. It is **never** sent to an LLM as-is; only the synthesized
prose in `.spc/profile.md` is, plus the bounded excerpts described below.

## File shape

```json
{
  "schema": 2,
  "sources": {
    "git": {
      "schema": 2,
      "source": "git",
      "mined_at": 1737000000.0,
      "evidence": { "...": "the git block below" }
    },
    "github": { "...": "PR/comment mining, see README" }
  }
}
```

Schema 1 (statistics only) is still readable: it loads as a statistics-only
block, so an upgrade never forces a re-mine before the next review reads the
profile. Schema 2 adds `commits`, `relationships`, `excerpts_enabled`.

A complete generated file lives in
[docs/example-evidence.json](example-evidence.json) — mined from a throwaway
five-commit repo whose story is: a feature lands touching two files, a follow-up
touches the same two, a fix follows in both, then a cache feature is reverted.
That fixture produces all four relationship kinds — one `revert`, one
`fix_after_change` hypothesis, one `fix_without_test`, and one `co_change` pair
— and it parses through the same loader `spc review` uses. SHAs and timestamps
differ per run, so treat them as illustrative.

## Git block

```json
{
  "commits_scanned": 2000,
  "hotspots": [["src/app/", 214], ["src/billing/invoice.py", 96]],
  "bug_classes": [{"keyword": "fix", "count": 769, "top_files": ["src/modeling.py"]}],
  "test_dirs": ["tests/"],
  "test_co_change_ratio": 0.48,
  "language_proportions": {"python": 75.6, "markdown": 12.1},
  "total_files_tracked": 4211,
  "excerpts_enabled": true,
  "relationships": [
    {
      "kind": "revert",
      "observed": true,
      "commits": ["4f2a...", "9c81..."],
      "files": ["src/billing/invoice.py"],
      "note": "revert of \"feat: batch invoice totals\""
    },
    {
      "kind": "fix_after_change",
      "observed": false,
      "commits": ["9c81...", "77de..."],
      "files": ["src/billing/invoice.py"],
      "note": "fix \"fix: rounding on invoice totals\" follows \"feat: batch invoice totals\" touching src/billing/invoice.py"
    }
  ],
  "commits": [
    {
      "sha": "9c8104f8c1...",
      "subject": "fix: rounding on invoice totals",
      "author_timestamp": 1730750400,
      "committer_timestamp": 1730754000,
      "files": [
        {"path": "src/billing/invoice.py", "change_type": "modified", "added": 4, "deleted": 2, "old_path": null}
      ],
      "excerpt": "diff --git a/src/billing/invoice.py b/src/billing/invoice.py\n@@ -112,3 +112,4 @@\n-    return round(total, 2)\n+    return Decimal(total).quantize(CENTS)\n",
      "references": ["412", "77"]
    }
  ]
}
```

### Fields

| Field | Meaning |
|---|---|
| `commits_scanned` | Commits inside the mined window (`learn_max_commits`). |
| `hotspots` | Top 20 most-changed files/dirs, noise excluded. |
| `bug_classes` | Frequency of bug keywords in subjects, with the files they cluster in. A *keyword count*, not a root-cause claim. |
| `test_dirs`, `test_co_change_ratio` | Where tests live; how often source commits also touch tests. |
| `language_proportions` | Rough file-extension mix of the tracked tree. |
| `total_files_tracked` | Size of the tracked tree, for context on the proportions. |
| `excerpts_enabled` | Whether this mine collected code excerpts. |
| `relationships` | See kinds below. `revert`/`fix_*` cite the exact commits; `co_change` cites the three most recent supporting commits (up to 10 pairs per mine). |
| `commits` | Only commits cited by a relationship (cap `MAX_PERSISTED_COMMITS`). |
| `commits[].files` | Changed files with `change_type` and `added`/`deleted` counts. Renames carry `old_path`. Capped at `MAX_PERSISTED_FILES`. |
| `commits[].files_total` | True number of files touched, even when the list above is capped. |
| `commits[].excerpt` | Bounded, secret-redacted `+`/`-` lines from that commit (see caps). |
| `commits[].references` | Issue/PR numbers and SHAs mentioned in the message. |

`commits` intentionally holds only cited commits — storing every record in a
2 000-commit window would bloat the artifact for no extra reviewer value.

## Relationship kinds

Every relationship carries `observed: bool`, which is a promise about the
evidence, not about certainty:

| Kind | Observed? | What it claims |
|---|---|---|
| `revert` | **yes** | Git recorded a `This reverts commit …` trailer naming the target. A hand-written `Revert "…"` subject with no trailer is *not* asserted as a link — the quoted subject cannot identify which commit was undone. |
| `fix_without_test` | **yes** | The commit's subject has a bug keyword and the commit touched no test file. The absence is a fact; whether tests were *needed* is left to the profile. |
| `co_change` | **yes** | These files appeared together in ≥3 commits. Correlation only — the note says they move together, never that one depends on the other. |
| `fix_after_change` | **no** | A bug-keyword commit modified code that an earlier commit had also modified, within 180 days. A *hypothesis* about causation. |

A commit containing the word "fix" is not evidence of a root cause. Signals
that would guess (e.g. "this migration was probably forgotten") are deliberately
not mined: an invented explanation is worse than no explanation, because the
profile reads as authoritative.

## What the synthesis prompt actually receives

A 2 000-commit window on a large repository yields far more relationships than
one prompt should carry (117 on transformers), so the renderer spends its budget
by explanatory value, not by detector order:

- **Change cascades get their own section, first.** Relationships are grouped by
  the earlier commit they follow; a parent with ≥2 follow-ups is a cascade and
  is stated with its count and the files that needed follow-up. This is the one
  claim a reader cannot cheaply re-derive (`git log --oneline` will not tell you
  that a cache rename cost six later fixes).
- **Blind reverts collapse to one line.** A revert whose target lies outside the
  scanned window and which carried no files says only "something was undone";
  41 such entries once consumed over half the section budget.
- **Observed evidence outranks hypotheses.** Reverts and co-changes are facts
  git asserts; a "this fix follows that change" link is a guess from file overlap
  in time. Ranked the other way, verbose hypotheses crowded out every observed
  revert, and the synthesis — reading the absence as a fact — wrote that the
  repo's reverts carry no file details. That sentence was false and traceable to
  the ordering.
- **`co_change` lines name the file pair.** The pair is the finding — a line
  with only a commit count discards it — and a pair seen in both directions is
  printed once.
- **Event lines are composed from structured fields, not the stored note.** The
  note repeats subject and files; rendering both truncated every subject twice
  and made a hypothesis line longer than an observed one.
- **Excerpts go to cascade parents and their fixes first.** Citation order is
  detector order, so reverts are cited before any fix; spending the 60 excerpt
  slots that way left the cascades undocumented.

Measured effect on the transformers profile: distinct file paths named rose
from 3 to 15, and the cache-rename cascade appears as the profile's first
bullet with its count verbatim.

## GitHub mining is incremental by identity

Merged PRs are skipped by number, not by a high-water mark. This matters
because a run can stop early — `github_max_prs`, `github_max_comments`, the
per-run API-call ceiling, or a rate limit — and a high-water mark makes
truncation permanent: it records the newest PR number it saw and every later
run skips everything below it, so the un-reached middle of that range is never
mined again. Skipping by identity instead records exactly what was stored; the
next run walks the same newest-first order, skips those PRs, and continues
into the range the truncated run never reached. Measured on transformers: a
truncated run stored 147 of 194 merged PRs in a range and stranded the rest;
the identity walk recovered all of them across two runs.

Comments are fetched from the repo-wide `/pulls/comments` endpoint (100 per
call) and filtered to the PRs just mined, rather than one call per PR. At 1
call per PR a 60-call budget covered 27 comments; at 100 per call it covers
1000, which is what makes `github_max_comments` reachable at all.

## Caps and why they exist

| Constant | Value | Rationale |
|---|---|---|
| `MAX_PERSISTED_COMMITS` | 200 | Keeps `evidence.json` in the tens-of-KB range. |
| `MAX_PERSISTED_FILES` | 50 | A commit touching thousands of files stores its first 50 paths plus a true `files_total`. |
| `MAX_EXCERPT_COMMITS` | 60 | Bounds `git show` calls per mine. |
| `MAX_EXCERPT_LINES` | 12 | Enough to show a failure's shape; not enough to leak a whole file. |
| `MAX_EXCERPT_FILES` | 5 | A rename of hundreds of files must not emit hundreds of `diff --git` headers. |
| `MAX_EXCERPT_COMMIT_BYTES` | 4 000 | One minified line can be megabytes; the byte cap holds regardless. |
| `MAX_EXCERPT_BYTES` | 200 000 | Budget across all excerpts (an excerpt is never split, so the total can exceed it by less than `MAX_EXCERPT_COMMIT_BYTES`). |
| `MAX_FIX_RELATIONSHIPS` | 25 | Prevents a noisy window from dominating the profile. |
| `MAX_CO_CHANGE_PAIRS` | 10 | Same — top pairs only. |
| `MAX_CO_CHANGE_FILES_PER_COMMIT` | 12 | A commit touching more files links everything to everything ("we ran a formatter"), which is not evidence. |
| `MAX_CITED_CO_CHANGE_COMMITS` | 3 | A co-change cites its three most recent supporting commits, not all of them. |
| `CO_CHANGE_MIN_SUPPORT` | 3 | Two files touched twice together is coincidence. |
| `FIX_FOLLOWUP_MAX_DAYS` | 180 | A "fix" six months later is a different change, not a follow-up. |

## Performance

Mining is **two `git` calls regardless of repo size**: one `git log --numstat`
for the window, one `git ls-files`. Excerpts add one `git show` per cited commit
(capped at 60).

Measured on a 2 000-commit window of a large repository (transformers,
~4 200 tracked files): **~3.7 s metadata-only, ~4.7 s with excerpts** — the
excerpt pass costs about one extra second for 59 `git show` calls. Deterministic
and network-free. Cost scales with `learn_max_commits`, not with repository size,
and mining never runs in the push path — only `spc init` and `spc learn`.

## Privacy

Excerpts are the only historical *code* the tool ever reads, and they are
handled differently from a live review diff:

- **Both sides are redacted.** A historical fix usually shows a secret being
  *removed*, so the `-` lines are as sensitive as the `+` lines. `spc learn`
  therefore scans and redacts added *and* removed lines, plus private-key block
  bodies (bounded to 128 lines after the header — `ssh-keygen -b 8192` produces
  about 91).
- **Redaction runs before trimming.** Trimming drops unchanged context lines,
  and a key rotation leaves `-----BEGIN …` as context while the key material
  sits on the changed lines. Scrubbing after trimming would remove the header
  that anchors body redaction and let the key through.
- **Headerless key material is still recognised.** `--unified=1` context is one
  line, so a key rotated deep inside a file shows no `-----BEGIN` at all. Inside
  a file section that plausibly holds a key — key-ish path/extension, or a run of
  consecutive base64 lines — long base64 lines are redacted on shape alone.
  Requiring both letter cases keeps hex digests out. The rule is confined to
  key-like sections so it does not redact ordinary code — the cost is that a
  section full of long base64 blobs (a batch of image fixtures, say) also
  qualifies, so its changed lines get redacted. That trade is deliberate:
  over-redacting an excerpt loses evidence, leaking a key cannot be undone.
  Excerpt path only: the push path keeps its "added lines matching a known
  pattern" contract, because a shape rule there would fire on every base64
  fixture in a test suite and teach people to ignore the warning.
- **Two shapes are scrubbed on both paths, in every file.** A hunk header's
  context suffix (`@@ … @@ <neighbouring line>`) is a copy of a line that appears
  nowhere else in the diff, so no line-level rule sees it; it is now scrubbed
  unconditionally, pattern secrets and key material alike. Base64-wrapped PEM
  material (`tls.key: LS0tLS1CRUdJi…`, the shape a TLS key takes in a k8s Secret
  or CI variable) is recognised by its encoded `-----BEGIN` prefix and redacted
  wherever it appears. A secret does not become harmless because the file is
  called `config.py`.
- **Bounded.** 12 changed lines per commit, 5 file headers, 4 KB per excerpt,
  60 commits, 200 KB total (a whole excerpt is never split, so the total can
  overshoot by less than one 4 KB excerpt).
- **Opt-out.** `[learn] send_code_excerpts = false` mines metadata only — no
  `git show` calls, no code anywhere in the artifact.
- **Never persisted raw into the profile.** The synthesis prompt gets the
  excerpts with an explicit instruction never to quote them, and the synthesized
  profile is filtered afterwards: any line reproducing a long run of excerpt
  code — from any excerpt, not just the first ones — is dropped before the
  profile is written. The profile is what every later review sends to the
  model, so a quote would travel much further than the one-off learn call.

Deleted secrets are still in your git history — mining does not change that.
Rotate them; SpiritCuffs only makes sure it does not copy them forward.
