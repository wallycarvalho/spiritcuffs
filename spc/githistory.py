"""Source A — local git mining: hotspots, bug classes, structured commit evidence.

Two layers, deliberately separated:

* **Statistics** (hotspots, bug classes, test co-change, languages) — unchanged
  from v1, retained so existing profiles keep their grounding.
* **Evidence** (this module's new half) — bounded per-commit records plus
  *relationships* between commits, so the profile can explain *why* a change
  caused a problem and how it was corrected.

Epistemic rule: every relationship carries ``observed`` — True when git data
states the link (a revert trailer, a file co-change count), False when it is a
heuristic (a fix touching files an earlier commit touched). A subject
containing "fix" is a keyword, never a root cause; ``observed=False``
relationships say "these commits may be related", not "this caused that".

Bounded by design: the full commit list lives in memory during mining, but only
relationship-involved commits are persisted, and diff excerpts are opt-in
(``learn.send_code_excerpts``), capped, and secret-redacted.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field, replace
from pathlib import Path

from spc.diff import run_git
from spc.secrets import is_change_line, is_diff_file_header, scrub_diff_text

# Vendored / lockfile / generated noise excluded from hotspots and co-change stats.
EXCLUDED_PATH_PATTERNS = re.compile(
    r"(^|/)("
    r"vendor/|vendors/|node_modules/|\.venv/|venv/|dist/|build/|target/"
    r"|__pycache__/|\.next/|\.turbo/|coverage/|\.min\.js$|\.min\.css$"
    r")",
    re.IGNORECASE,
)
LOCKFILE_NAMES = {
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "Cargo.lock",
    "poetry.lock", "uv.lock", "Pipfile.lock", "composer.lock", "Gemfile.lock",
}

BUG_KEYWORDS = ("fix", "bugfix", "hotfix", "revert", "bug")
COMMIT_SUBJECT_SPLIT = re.compile(r"[\s:;\-–—()]+")

TOP_HOTSPOT_COUNT = 20

# --- evidence caps -----------------------------------------------------------
# Persisted commit records: only relationship-involved commits, hard ceiling.
MAX_PERSISTED_COMMITS = 200
# File entries stored per record (a 3 000-file commit must not bloat the file).
MAX_PERSISTED_FILES = 50
# Commits that receive a diff excerpt (a subset of the above).
MAX_EXCERPT_COMMITS = 60
# Lines of +/- content per excerpt (hunk headers excluded from the count).
MAX_EXCERPT_LINES = 12
# File headers per excerpt — a 500-file rename would otherwise emit 500 lines.
MAX_EXCERPT_FILES = 5
# Bytes per single excerpt, so one minified line cannot consume the budget.
MAX_EXCERPT_COMMIT_BYTES = 4_000
# Total excerpt budget for the whole evidence file.
MAX_EXCERPT_BYTES = 200_000
# Relationship detector caps.
MAX_FIX_RELATIONSHIPS = 25
MAX_CO_CHANGE_PAIRS = 10
# A commit touching more files than this links everything to everything — its
# pairs are "we ran a formatter", not evidence that the files belong together.
MAX_CO_CHANGE_FILES_PER_COMMIT = 12
MAX_CITED_CO_CHANGE_COMMITS = 3
CO_CHANGE_MIN_SUPPORT = 3
# A "follow-up" fix must land within this window of the change it may correct.
FIX_FOLLOWUP_MAX_DAYS = 180

# One ``git log`` call yields per-commit headers *and* numstat, so mining never
# pays a per-commit subprocess. ``%x00`` starts a record, ``%x1f`` separates
# fields, ``%x1e`` ends the header block so the numstat lines that follow cannot
# be mistaken for header content. Parents come BEFORE the body: a body is
# unconstrained text, so any ``%x1f``/``%x1e`` inside it damages only itself,
# whereas a corrupted ``%P`` would silently turn a merge into an ordinary
# commit (and merges must stay out of relationship detection).
LOG_FORMAT = "--format=%x00%H%x1f%at%x1f%ct%x1f%s%x1f%P%x1f%b%x1e"

REVERT_TRAILER = re.compile(r"This reverts commit ([0-9a-f]{7,40})", re.IGNORECASE)
REVERT_SUBJECT = re.compile(r'^Revert "(.+)"')
REFERENCE_PATTERN = re.compile(r"(?<![\w/])(?:#(\d+)|([0-9a-f]{7,40})\b)")

RELATIONSHIP_REVERT = "revert"
RELATIONSHIP_FIX_AFTER_CHANGE = "fix_after_change"
RELATIONSHIP_FIX_WITHOUT_TEST = "fix_without_test"
RELATIONSHIP_CO_CHANGE = "co_change"


@dataclass(frozen=True)
class FileChange:
    """One file touched by a commit, with its change type and line counts."""

    path: str
    change_type: str  # "added" | "modified" | "deleted" | "renamed"
    added: int = 0
    deleted: int = 0
    old_path: str | None = None


@dataclass(frozen=True)
class CommitRecord:
    """Structured record of one commit (metadata + optional bounded excerpt).

    ``body`` and ``parents`` are mined for relationship detection and never
    persisted: commit bodies are unbounded, and parent SHAs only matter while
    the scanned window is in memory.
    """

    sha: str
    subject: str
    author_timestamp: int
    committer_timestamp: int
    files: tuple[FileChange, ...] = ()
    excerpt: str = ""
    body: str = ""
    references: tuple[str, ...] = ()
    parents: tuple[str, ...] = ()
    # True count before ``files`` was capped for storage; 0 means "len(files)".
    files_total: int = 0

    def paths(self) -> list[str]:
        """All paths the commit touched (rename-aware)."""
        return [change.path for change in self.files]


@dataclass(frozen=True)
class Relationship:
    """A potential link between commits, plus the evidence that supports it.

    ``observed`` is the epistemic marker: True when git data asserts the link
    (revert trailer, repeated co-change), False when the link is a heuristic
    and must be presented as a possibility, never as a cause.
    """

    kind: str
    observed: bool
    commits: tuple[str, ...]
    files: tuple[str, ...] = ()
    note: str = ""


@dataclass
class GitEvidence:
    """Raw, JSON-serializable statistics + evidence mined from ``git log``."""

    commits_scanned: int = 0
    hotspots: list[tuple[str, int]] = field(default_factory=list)
    bug_classes: list[dict[str, object]] = field(default_factory=list)
    test_dirs: list[str] = field(default_factory=list)
    test_co_change_ratio: float = 0.0
    language_proportions: dict[str, float] = field(default_factory=dict)
    total_files_tracked: int = 0
    # Evidence layer (schema 2 of the git source).
    commits: list[CommitRecord] = field(default_factory=list)
    relationships: list[Relationship] = field(default_factory=list)
    excerpts_enabled: bool = False


def mine_git_history(
    repo_root: Path, max_commits: int, *, collect_excerpts: bool = True
) -> GitEvidence | None:
    """Mine the repo's history into :class:`GitEvidence`. None when git fails / empty repo.

    *collect_excerpts* mirrors ``learn.send_code_excerpts``: when False no diff
    content is fetched or stored at all (metadata and relationships only).
    """
    log = run_git(
        repo_root,
        "log",
        f"--max-count={max_commits}",
        "--numstat",
        "-M",
        LOG_FORMAT,
        "--",
        decode_errors="replace",
    )
    if log is None:
        return None

    commits = _parse_log(log)
    if not commits:
        return None

    tracked = run_git(repo_root, "ls-files", decode_errors="replace")
    all_files = tracked.splitlines() if tracked else []

    evidence = GitEvidence(commits_scanned=len(commits), excerpts_enabled=collect_excerpts)
    evidence.hotspots = _hotspots(commits)
    evidence.bug_classes = _bug_classes(commits)
    evidence.test_dirs, evidence.test_co_change_ratio = _test_layout(commits)
    evidence.language_proportions = _languages(all_files or [p for c in commits for p in c.paths()])
    evidence.total_files_tracked = len(all_files)

    evidence.relationships = detect_relationships(commits)
    evidence.commits = _commits_in_relationships(
        commits, evidence.relationships, repo_root if collect_excerpts else None
    )
    return evidence


def _parse_log(log: str) -> list[CommitRecord]:
    """Parse a ``LOG_FORMAT`` + ``--numstat`` dump into commit records.

    Per commit git emits ``\\x00`` + ``\\x1f``-separated header fields (SHA,
    author date, committer date, subject, parents, body), the ``\\x1e`` header
    terminator, then a blank line and the numstat body for that same commit.
    Separating on ``\\x00`` therefore yields one segment per commit; inside it,
    ``\\x1e`` ends the header and everything after is numstat.
    """
    commits: list[CommitRecord] = []
    for segment in log.split("\x00")[1:]:
        header, separator, body_block = segment.partition("\x1e")
        if not separator:
            continue
        fields = header.split("\x1f")
        if len(fields) < 4:
            continue
        sha, author_ts, committer_ts, subject = fields[:4]
        parents_raw = fields[4] if len(fields) > 4 else ""
        message_body = fields[5] if len(fields) > 5 else ""
        commits.append(
            CommitRecord(
                sha=sha,
                subject=subject,
                author_timestamp=_int_or_zero(author_ts),
                committer_timestamp=_int_or_zero(committer_ts),
                files=tuple(_parse_numstat_files(body_block)),
                body=message_body.strip(),
                references=_extract_references(f"{subject}\n{message_body}"),
                parents=tuple(parents_raw.split()),
            )
        )
    return commits


def _extract_references(text: str) -> tuple[str, ...]:
    """Collect issue/PR numbers and commit SHAs mentioned in a message."""
    found: list[str] = []
    for match in REFERENCE_PATTERN.finditer(text):
        value = match.group(1) or match.group(2)
        if value and value not in found:
            found.append(value)
    return tuple(found[:8])


def _parse_numstat_files(body: str) -> list[FileChange]:
    """Parse the numstat block of one record into :class:`FileChange` entries."""
    changes: list[FileChange] = []
    for line in body.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        added_raw, deleted_raw, raw_path = parts[0], parts[1], parts[2]
        added = _int_or_zero(added_raw)
        deleted = _int_or_zero(deleted_raw)
        rename = _split_rename(raw_path)
        if rename is not None:
            old_path, new_path = rename
            changes.append(
                FileChange(
                    path=new_path,
                    change_type="renamed",
                    added=added,
                    deleted=deleted,
                    old_path=old_path,
                )
            )
            continue
        changes.append(
            FileChange(
                path=raw_path,
                change_type=_change_type(added_raw, deleted_raw),
                added=added,
                deleted=deleted,
            )
        )
    return changes


def _split_rename(raw_path: str) -> tuple[str, str] | None:
    """Split git's rename spellings into ``(old_path, new_path)``; None if not a rename.

    git uses two forms: the compact ``src/{old.py => new.py}`` (only the
    differing part is braced) and the plain ``old.py => new.py`` used when the
    whole path changed.
    """
    braced = re.match(r"^(.*)\{(.*) => (.*)\}(.*)$", raw_path)
    if braced:
        prefix, old_middle, new_middle, suffix = braced.groups()
        return f"{prefix}{old_middle}{suffix}", f"{prefix}{new_middle}{suffix}"
    if " => " in raw_path:
        old_path, _, new_path = raw_path.partition(" => ")
        return old_path, new_path
    return None


def _change_type(added_raw: str, deleted_raw: str) -> str:
    """Infer added/deleted/modified from numstat counts (git omits status here)."""
    binary = added_raw == "-" or deleted_raw == "-"
    added = _int_or_zero(added_raw)
    deleted = _int_or_zero(deleted_raw)
    if binary:
        return "modified"
    if deleted == 0 and added > 0:
        return "added"
    if added == 0 and deleted > 0:
        return "deleted"
    return "modified"


def _int_or_zero(raw: str) -> int:
    """Parse an integer field, treating git's binary marker ``-`` as 0."""
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def _is_noise(path: str) -> bool:
    """True for vendored / lockfile / generated paths excluded from stats."""
    name = path.rsplit("/", 1)[-1]
    return bool(EXCLUDED_PATH_PATTERNS.search(path)) or name in LOCKFILE_NAMES


def _hotspots(commits: list[CommitRecord]) -> list[tuple[str, int]]:
    """Top-20 most-changed files AND directories (one level), noise-excluded."""
    file_counts: Counter[str] = Counter()
    dir_counts: Counter[str] = Counter()
    for commit in commits:
        for path in commit.paths():
            if _is_noise(path):
                continue
            file_counts[path] += 1
            top_dir = path.split("/")[0]
            if "/" in path:
                dir_counts[top_dir + "/"] += 1
    combined: Counter[str] = Counter()
    combined.update(file_counts)
    combined.update(dir_counts)
    return combined.most_common(TOP_HOTSPOT_COUNT)


def _bug_classes(commits: list[CommitRecord]) -> list[dict[str, object]]:
    """Frequency + top file clusters of fix/hotfix/revert commits, noise-excluded.

    Keyword frequency only — this is a *statistic*, not evidence that any
    particular fix corrected any particular change. See ``detect_relationships``
    for links that carry their supporting commits.
    """
    bug_files: Counter[str] = Counter()
    keyword_counts: Counter[str] = Counter()
    for commit in commits:
        matched = _bug_keywords(commit.subject)
        if not matched:
            continue
        for keyword in matched:
            keyword_counts[keyword] += 1
        for path in commit.paths():
            if not _is_noise(path):
                bug_files[path] += 1
    return [
        {
            "keyword": keyword,
            "count": keyword_counts[keyword],
            "top_files": [path for path, _ in bug_files.most_common(5)],
        }
        for keyword in ("fix", "hotfix", "revert")
        if keyword_counts[keyword]
    ]


def _bug_keywords(subject: str) -> list[str]:
    """Bug-ish keywords present in a commit subject (whole-word match)."""
    words = {word.lower() for word in COMMIT_SUBJECT_SPLIT.split(subject)}
    return [keyword for keyword in BUG_KEYWORDS if keyword in words]


TEST_DIR_SEGMENTS = frozenset({"test", "tests", "__tests__", "spec", "specs", "testing"})
TEST_FILE_PATTERNS = re.compile(
    r"(?:^test_.*\.py$|.*_test\.py$|.*_tests\.py$|.*\.(?:test|spec)\.[cm]?[jt]sx?$)"
)


def _is_test_path(path: str) -> bool:
    """Heuristic: does this path look like a test file/dir?

    ``fix_without_test`` claims this as an observed fact, so the check follows
    the layouts real projects use (``tests/``, ``__tests__/``, ``spec/``,
    ``test_*.py``, ``*_test.py``, ``*.spec.ts``) rather than a bare substring —
    a bare ``"test" in name`` misses ``src/foo.spec.ts`` and wrongly matches
    ``src/latest.py``.
    """
    segments = path.lower().split("/")
    if TEST_DIR_SEGMENTS.intersection(segments[:-1]):
        return True
    return bool(TEST_FILE_PATTERNS.match(segments[-1]))


def _test_layout(commits: list[CommitRecord]) -> tuple[list[str], float]:
    """(test dirs, source↔test co-change ratio).

    Co-change ratio: of commits touching source files, the fraction that also
    touched at least one test file.
    """
    test_dir_counts: Counter[str] = Counter()
    source_commits = 0
    source_and_test_commits = 0
    for commit in commits:
        test_paths = [p for p in commit.paths() if _is_test_path(p)]
        source_paths = [p for p in commit.paths() if not _is_test_path(p) and not _is_noise(p)]
        for path in test_paths:
            top = path.split("/")[0]
            test_dir_counts[top + "/" if "/" in path else "(root)"] += 1
        if source_paths:
            source_commits += 1
            if test_paths:
                source_and_test_commits += 1
    ratio = (
        round(source_and_test_commits / source_commits, 3)
        if source_commits
        else 0.0
    )
    dirs = [d for d, _ in test_dir_counts.most_common(5)]
    return dirs, ratio


def _languages(all_files: list[str]) -> dict[str, float]:
    """Rough language proportions across tracked files."""
    from spc.languages import language_proportions

    return language_proportions([f for f in all_files if f and not _is_noise(f)])


# --- relationship detection --------------------------------------------------


def detect_relationships(commits: list[CommitRecord]) -> list[Relationship]:
    """Find potential links between commits; newest-first, capped per detector.

    Ordering in *commits* is git's default (newest first). Detectors:

    * ``revert`` — observed: git's own ``This reverts commit …`` trailer
    * ``fix_after_change`` — hypothesis: a fix sharing files with an earlier commit
    * ``fix_without_test`` — observed: the fix commit changed no test file
    * ``co_change`` — observed: file pairs that repeatedly change together

    Only the scanned window is examined; history beyond it is out of scope.
    """
    by_sha = {commit.sha: commit for commit in commits}
    relationships: list[Relationship] = []
    relationships.extend(_revert_relationships(commits, by_sha))
    relationships.extend(_fix_followups(commits))
    relationships.extend(_fix_without_tests(commits))
    relationships.extend(_co_change_pairs(commits))
    return relationships


def _revert_relationships(
    commits: list[CommitRecord], by_sha: dict[str, CommitRecord]
) -> list[Relationship]:
    """Reverts and the commits they undo — observed, from git's own data.

    A revert whose target falls outside the scanned window is still reported:
    git's trailer states the fact, and dropping it would hide exactly the kind
    of event the profile needs. Only the reverted subject is then unknown.
    """
    relationships: list[Relationship] = []
    for commit in commits:
        raw_sha = _trailer_sha(commit)
        if raw_sha is None:
            continue
        target_sha = _resolve_sha(raw_sha, by_sha)
        target = by_sha.get(target_sha) if target_sha else None
        relationships.append(
            Relationship(
                kind=RELATIONSHIP_REVERT,
                observed=True,
                commits=(commit.sha, target.sha) if target else (commit.sha,),
                files=tuple(target.paths()[:10]) if target else (),
                note=(
                    f'revert of "{_short(target.subject)}"' if target
                    else f"revert of a commit outside the scanned window "
                    f"({raw_sha[:12]})"
                ),
            )
        )
    return relationships


def _trailer_sha(commit: CommitRecord) -> str | None:
    """The SHA a ``This reverts commit …`` trailer names, as written; None if absent.

    Only git's own trailer counts. Guessing from a quoted subject would upgrade
    a guess to ``observed=True`` — and a hand-written ``Revert "wip"`` matches
    the newest commit with that subject, not the one actually reverted. Such a
    revert keeps its subject in the record; it is simply not asserted as a link.
    """
    trailer = REVERT_TRAILER.search(commit.subject) or REVERT_TRAILER.search(commit.body)
    return trailer.group(1) if trailer is not None else None


def _resolve_sha(raw_sha: str, by_sha: dict[str, CommitRecord]) -> str | None:
    """Resolve a short SHA from a trailer against the scanned commits."""
    for sha in by_sha:
        if sha.startswith(raw_sha):
            return sha
    return None


def _fix_followups(commits: list[CommitRecord]) -> list[Relationship]:
    """Hypothesis: a fix that likely corrects an earlier change to the same files.

    The link is *file overlap within a time window* — it says two commits touch
    the same code, never that one caused the other's problem. Callers must
    present these as possibilities (``observed=False``).

    The earlier commit must have *changed existing* code (modified/renamed), not
    merely created the file: otherwise every early fix would be reported as a
    follow-up to the initial import, which is noise rather than signal.
    """
    relationships: list[Relationship] = []
    newer_first = list(commits)  # git log order: newest first
    existing_paths = {
        commit.sha: _changed_existing_paths(commit) for commit in newer_first
    }
    for index, commit in enumerate(newer_first):
        if not _is_fix(commit):
            continue
        fix_paths = {p for p in commit.paths() if not _is_noise(p)}
        if not fix_paths:
            continue
        previous = _nearest_overlapping(
            newer_first[index + 1:], commit, fix_paths, existing_paths
        )
        if previous is None:
            continue
        overlap = sorted(fix_paths & existing_paths[previous.sha])
        relationships.append(
            Relationship(
                kind=RELATIONSHIP_FIX_AFTER_CHANGE,
                observed=False,
                commits=(commit.sha, previous.sha),
                files=tuple(overlap[:10]),
                note=(
                    f'fix "{_short(commit.subject)}" follows '
                    f'"{_short(previous.subject)}" touching {_describe_paths(overlap)}'
                ),
            )
        )
        if len(relationships) >= MAX_FIX_RELATIONSHIPS:
            break
    return relationships


def _changed_existing_paths(commit: CommitRecord) -> set[str]:
    """Paths this commit *modified*, excluding files it created or deleted."""
    return {
        change.path
        for change in commit.files
        if change.change_type in ("modified", "renamed") and not _is_noise(change.path)
    }


def _nearest_overlapping(
    candidates: list[CommitRecord],
    fix_commit: CommitRecord,
    fix_paths: set[str],
    existing_paths: dict[str, set[str]],
) -> CommitRecord | None:
    """Most recent earlier commit that modified a file *fix_commit* also touched.

    *existing_paths* is precomputed once per mine: rebuilding the path set for
    every candidate inside this loop made relationship detection quadratic.
    """
    for candidate in candidates:
        if _is_merge(candidate) or _is_revert(candidate):
            continue
        if not (fix_paths & existing_paths.get(candidate.sha, set())):
            continue
        if not _within_days(candidate, fix_commit, FIX_FOLLOWUP_MAX_DAYS):
            continue
        return candidate
    return None


def _is_merge(commit: CommitRecord) -> bool:
    """True for merge commits — their numstat repeats work authored elsewhere."""
    return len(commit.parents) > 1


def _within_days(older: CommitRecord, newer: CommitRecord, max_days: int) -> bool:
    """True when *older* precedes *newer* by no more than *max_days*."""
    gap_seconds = abs(newer.committer_timestamp - older.committer_timestamp)
    return gap_seconds <= max_days * 86400


def _is_fix(commit: CommitRecord) -> bool:
    """Whole-word bug keyword, excluding reverts — a keyword, not a root cause.

    Reverts are their own relationship kind; counting them as fixes would also
    report an undo as a "fix that skipped tests", which is noise.
    """
    if _is_revert(commit):
        return False
    return bool(_bug_keywords(commit.subject))


def _is_revert(commit: CommitRecord) -> bool:
    """True when the commit is a git revert (trailer or ``Revert "..."`` subject)."""
    return (
        REVERT_TRAILER.search(commit.subject) is not None
        or REVERT_TRAILER.search(commit.body) is not None
        or REVERT_SUBJECT.match(commit.subject) is not None
    )


def _fix_without_tests(commits: list[CommitRecord]) -> list[Relationship]:
    """Observed: fix commits that changed source files but no test file.

    The observation is factual (no test file was in the commit). Whether tests
    were *needed* is a judgement the profile must make, not this module.
    """
    relationships: list[Relationship] = []
    for commit in commits:
        if not _is_fix(commit):
            continue
        paths = commit.paths()
        source_paths = [p for p in paths if not _is_test_path(p) and not _is_noise(p)]
        if not source_paths or any(_is_test_path(p) for p in paths):
            continue
        relationships.append(
            Relationship(
                kind=RELATIONSHIP_FIX_WITHOUT_TEST,
                observed=True,
                commits=(commit.sha,),
                files=tuple(source_paths[:10]),
                note=(
                    f'fix "{_short(commit.subject)}" changed no test file '
                    f"({_describe_paths(source_paths)})"
                ),
            )
        )
        if len(relationships) >= MAX_FIX_RELATIONSHIPS:
            break
    return relationships


def _co_change_pairs(commits: list[CommitRecord]) -> list[Relationship]:
    """Observed: file pairs that repeatedly appear in the same commit.

    Support-counted co-change is a correlation, not a dependency — the note
    says files move together and stops there. Merge commits cannot contribute:
    ``git log --numstat`` emits no per-file counts for a merge, so a merge
    record arrives with an empty file list.
    """
    pair_counts: Counter[tuple[str, str]] = Counter()
    pair_commits: dict[tuple[str, str], list[str]] = defaultdict(list)
    for commit in commits:
        files = sorted({p for p in commit.paths() if not _is_noise(p) and not _is_test_path(p)})
        if len(files) > MAX_CO_CHANGE_FILES_PER_COMMIT:  # bulk commits link everything
            continue
        for i, first in enumerate(files):
            for second in files[i + 1:]:
                pair_counts[(first, second)] += 1
                cited = pair_commits[(first, second)]
                # Commits arrive newest-first, so the first three appended are
                # the most recent support — the part worth citing.
                if len(cited) < MAX_CITED_CO_CHANGE_COMMITS:
                    cited.append(commit.sha)
    relationships: list[Relationship] = []
    for pair, count in pair_counts.most_common(MAX_CO_CHANGE_PAIRS):
        if count < CO_CHANGE_MIN_SUPPORT:
            break
        relationships.append(
            Relationship(
                kind=RELATIONSHIP_CO_CHANGE,
                observed=True,
                commits=tuple(pair_commits[pair]),
                files=pair,
                note=f"changed together in {count} commits",
            )
        )
    return relationships


def _short(subject: str, limit: int = 72) -> str:
    """Trim a subject for use inside a one-line note."""
    subject = " ".join(subject.split())
    return subject if len(subject) <= limit else subject[: limit - 1] + "…"


def _describe_paths(paths: list[str] | tuple[str, ...], limit: int = 3) -> str:
    """Comma-joined paths, with a ``+N more`` tail past the limit."""
    listed = ", ".join(paths[:limit])
    extra = len(paths) - limit
    return f"{listed} (+{extra} more)" if extra > 0 else listed


# --- persistence of the evidence layer ---------------------------------------


def _commits_in_relationships(
    commits: list[CommitRecord],
    relationships: list[Relationship],
    repo_root: Path | None,
) -> list[CommitRecord]:
    """Keep only commits cited by a relationship; attach bounded excerpts.

    Requirement 5: never persist whole histories. *repo_root* is None when
    excerpts are disabled — then no diff is fetched at all.
    """
    cited: list[str] = []
    seen: set[str] = set()
    for relationship in relationships:
        for sha in relationship.commits:
            if sha not in seen:
                seen.add(sha)
                cited.append(sha)
    by_sha = {commit.sha: commit for commit in commits}
    kept = [by_sha[sha] for sha in cited if sha in by_sha][:MAX_PERSISTED_COMMITS]
    if repo_root is None:
        return kept

    budget = MAX_EXCERPT_BYTES
    with_excerpts: list[CommitRecord] = []
    for index, commit in enumerate(kept):
        if index >= MAX_EXCERPT_COMMITS or budget <= 0:
            with_excerpts.append(commit)
            continue
        excerpt = _bounded_excerpt(repo_root, commit)
        if not excerpt:
            with_excerpts.append(commit)
            continue
        # One excerpt is already byte-capped, so the total can only overshoot
        # by less than MAX_EXCERPT_COMMIT_BYTES — never by a whole blob.
        budget -= len(excerpt.encode("utf-8"))
        with_excerpts.append(replace(commit, excerpt=excerpt))
    return with_excerpts


def _bounded_excerpt(repo_root: Path, commit: CommitRecord) -> str:
    """Fetch and bound one commit's diff hunks; secret-redacted. "" on failure.

    Redaction runs BEFORE trimming, over the whole diff. Trimming drops context
    lines, and a key rotation leaves ``-----BEGIN …`` as context while the key
    material sits on ``-``/``+`` lines — so scrubbing afterwards would strip the
    header that anchors body redaction and leak the key.

    Redaction covers BOTH diff sides (``scrub_diff_text``): unlike a live push,
    a historical fix often shows a secret being removed, and the value is
    equally sensitive on the ``-`` line.

    ``--no-textconv``/``--no-ext-diff`` keep a repo's ``.gitattributes`` from
    feeding arbitrary program output into the excerpt.
    """
    diff = run_git(
        repo_root,
        "show",
        "--format=",
        "--unified=1",
        "--no-color",
        "--no-textconv",
        "--no-ext-diff",
        commit.sha,
        "--",
        decode_errors="replace",
    )
    if not diff or not diff.strip():
        return ""
    excerpt = _trim_to_changed_lines(scrub_diff_text(diff))
    return _cap_bytes(excerpt, MAX_EXCERPT_COMMIT_BYTES)


def _cap_bytes(text: str, max_bytes: int) -> str:
    """Hard byte ceiling for one excerpt — a single minified line can be megabytes.

    The cut keeps the partial final line: for a minified bundle that one line is
    the entire evidence, so trimming back to the last newline would leave only
    headers and a ``-`` line that explain nothing.
    """
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    clipped = encoded[:max_bytes].decode("utf-8", errors="ignore")
    return clipped.rstrip("\n") + "\n…(truncated)"


def _trim_to_changed_lines(
    diff_text: str,
    max_lines: int = MAX_EXCERPT_LINES,
    max_files: int = MAX_EXCERPT_FILES,
) -> str:
    """Keep file headers, hunk headers, and at most *max_lines* changed lines.

    ``diff --git a/x b/x`` is kept because it is the only place the file path
    appears; ``index``/``---``/``+++`` carry nothing extra and are dropped so a
    multi-file commit stays inside the budget. A removed PEM header renders as
    ``-------BEGIN…`` and is content, not a file header (see ``is_diff_file_header``).

    Returns "" when nothing but headers remained: a binary file has no hunk, so
    its excerpt would be a bare path that tells the profile nothing.
    """
    kept: list[str] = []
    changed = 0
    files = 0
    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            if files >= max_files:
                kept.append("…(more files)")
                break
            files += 1
            kept.append(line)
            continue
        if line.startswith("@@"):
            kept.append(line)
            continue
        if is_diff_file_header(line) or line.startswith(("index ", "new file", "deleted file")):
            continue
        if is_change_line(line):
            if changed >= max_lines:
                break
            kept.append(line)
            changed += 1
    if changed == 0:
        return ""
    return "\n".join(kept)


# --- JSON schema of the git source -------------------------------------------
# Schema 1 = statistics only (v1). Schema 2 adds commits/relationships/excerpts.
# Readers accept both: a schema-1 block loads as statistics-only evidence, so an
# upgrade never requires a re-mine before the next review reads the profile.
GIT_EVIDENCE_SCHEMA = 2


def evidence_to_block(evidence: GitEvidence) -> dict[str, object]:
    """Serialize :class:`GitEvidence` into the ``.spc/evidence.json`` git block."""
    return {
        "commits_scanned": evidence.commits_scanned,
        "hotspots": evidence.hotspots,
        "bug_classes": evidence.bug_classes,
        "test_dirs": evidence.test_dirs,
        "test_co_change_ratio": evidence.test_co_change_ratio,
        "language_proportions": evidence.language_proportions,
        "total_files_tracked": evidence.total_files_tracked,
        "excerpts_enabled": evidence.excerpts_enabled,
        "commits": [_commit_to_dict(commit) for commit in evidence.commits],
        "relationships": [_relationship_to_dict(rel) for rel in evidence.relationships],
    }


def evidence_from_block(block: dict[str, object]) -> GitEvidence:
    """Deserialize a git evidence block of any known schema.

    Raises ValueError/TypeError on structurally broken input so callers can
    degrade to "no cached evidence" instead of crashing.
    """
    return GitEvidence(
        commits_scanned=int(block.get("commits_scanned", 0)),
        hotspots=[
            (str(path), int(count))
            for path, count in _as_list_of_pairs(block.get("hotspots"))
        ],
        bug_classes=[
            entry for entry in _as_list(block.get("bug_classes"))
            if isinstance(entry, dict)
        ],
        test_dirs=[str(value) for value in _as_list(block.get("test_dirs"))],
        test_co_change_ratio=float(block.get("test_co_change_ratio", 0.0)),
        language_proportions={
            str(key): float(value)
            for key, value in _as_dict(block.get("language_proportions")).items()
        },
        total_files_tracked=int(block.get("total_files_tracked", 0)),
        commits=[
            _commit_from_dict(entry)
            for entry in _as_list(block.get("commits"))
            if isinstance(entry, dict)
        ],
        relationships=[
            _relationship_from_dict(entry)
            for entry in _as_list(block.get("relationships"))
            if isinstance(entry, dict)
        ],
        excerpts_enabled=bool(block.get("excerpts_enabled", False)),
    )


def _commit_to_dict(commit: CommitRecord) -> dict[str, object]:
    """Serialize one commit record.

    ``files`` is capped: a commit touching thousands of files would otherwise
    bloat ``evidence.json`` on its own, and the extra paths add nothing the
    profile can use. ``files_total`` keeps the true count visible.
    """
    return {
        "sha": commit.sha,
        "subject": commit.subject,
        "author_timestamp": commit.author_timestamp,
        "committer_timestamp": commit.committer_timestamp,
        "files": [
            {
                "path": change.path,
                "change_type": change.change_type,
                "added": change.added,
                "deleted": change.deleted,
                "old_path": change.old_path,
            }
            for change in commit.files[:MAX_PERSISTED_FILES]
        ],
        "files_total": commit.files_total or len(commit.files),
        "excerpt": commit.excerpt,
        "references": list(commit.references),
    }


def _commit_from_dict(raw: dict[str, object]) -> CommitRecord:
    """Deserialize one commit record (malformed file entries are skipped)."""
    files = [
        FileChange(
            path=str(entry["path"]),
            change_type=str(entry.get("change_type", "modified")),
            added=int(entry.get("added", 0)),
            deleted=int(entry.get("deleted", 0)),
            old_path=str(entry["old_path"]) if entry.get("old_path") else None,
        )
        for entry in _as_list(raw.get("files"))
        if isinstance(entry, dict) and entry.get("path")
    ]
    return CommitRecord(
        sha=str(raw.get("sha", "")),
        subject=str(raw.get("subject", "")),
        author_timestamp=int(raw.get("author_timestamp", 0)),
        committer_timestamp=int(raw.get("committer_timestamp", 0)),
        files=tuple(files),
        excerpt=str(raw.get("excerpt", "")),
        references=tuple(str(ref) for ref in _as_list(raw.get("references"))),
        files_total=int(raw.get("files_total") or 0),
    )


def _relationship_to_dict(rel: Relationship) -> dict[str, object]:
    """Serialize one relationship."""
    return {
        "kind": rel.kind,
        "observed": rel.observed,
        "commits": list(rel.commits),
        "files": list(rel.files),
        "note": rel.note,
    }


def _relationship_from_dict(raw: dict[str, object]) -> Relationship:
    """Deserialize one relationship."""
    return Relationship(
        kind=str(raw.get("kind", "")),
        observed=bool(raw.get("observed", False)),
        commits=tuple(str(sha) for sha in _as_list(raw.get("commits"))),
        files=tuple(str(path) for path in _as_list(raw.get("files"))),
        note=str(raw.get("note", "")),
    )


def _as_list(value: object) -> list[object]:
    """Coerce a JSON value to a list (non-lists become empty)."""
    return value if isinstance(value, list) else []


def _as_dict(value: object) -> dict:
    """Coerce a JSON value to a dict (non-dicts become empty)."""
    return value if isinstance(value, dict) else {}


def _as_list_of_pairs(value: object) -> list[tuple[object, object]]:
    """Coerce ``[[k, v], ...]`` to pairs, skipping malformed entries."""
    pairs: list[tuple[object, object]] = []
    for entry in _as_list(value):
        if isinstance(entry, list | tuple) and len(entry) == 2:
            pairs.append((entry[0], entry[1]))
    return pairs

