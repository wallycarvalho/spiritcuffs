"""JSONL history append + ``spc history`` pretty-print. Collision-safe repo slugs."""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

from rich.console import Console
from rich.table import Table

from spc.diff import DiffResult, pathhash8

HISTORY_ROOT_ENV = "SPC_HISTORY_DIR"  # overridable for tests
DEFAULT_HISTORY_ROOT = Path.home() / ".spc" / "history"


def history_dir(repo_path: Path) -> Path:
    """History root (``~/.spc/history`` unless overridden for tests)."""
    override = os.environ.get(HISTORY_ROOT_ENV)
    if override:
        return Path(override)
    return DEFAULT_HISTORY_ROOT


def history_file(repo_path: Path) -> Path:
    """``<history_dir>/<repo-name>-<pathhash8>.jsonl`` for *repo_path*."""
    name = repo_path.resolve().name
    return history_dir(repo_path) / f"{name}-{pathhash8(repo_path)}.jsonl"


def append_record(
    repo_path: Path,
    *,
    diff_hash: str,
    diff: DiffResult,
    findings: list[dict],
    model: str,
    cached: bool = False,
    outcome: str = "passed",
) -> Path:
    """Append one review record; returns the file written."""
    record = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "diff_hash": diff_hash,
        "source": diff.source,
        "files": diff.files,
        "findings": findings,
        "model": model,
        "cached": cached,
        "outcome": outcome,
    }
    path = history_file(repo_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    return path


def read_records(repo_path: Path) -> list[dict]:
    """All records for this repo, oldest first. Corrupt lines are skipped."""
    path = history_file(repo_path)
    if not path.is_file():
        return []
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:  # permissions/race — fail soft; history is advisory
        return []
    records: list[dict] = []
    for line in content.splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def last_record_for_hash(repo_path: Path, diff_hash: str) -> dict | None:
    """Most recent record whose diff_hash matches — the cache-hit path."""
    for record in reversed(read_records(repo_path)):
        if record.get("diff_hash") == diff_hash:
            return record
    return None


def cache_hit_message(repo_path: Path, diff_hash: str) -> dict | None:
    """The record to serve as a cache hit — only successful reviews qualify.

    Records with ``outcome`` "failed"/"skipped" (LLM did not complete) are
    never cacheable: treating them as "reviewed, clean" would poison the
    cache with a non-review. "incomplete" is excluded too — a truncated
    diff that happened to come back clean is not a clean verdict for the
    full diff; the next push re-reviews for real.
    """
    record = last_record_for_hash(repo_path, diff_hash)
    if record is None:
        return None
    if str(record.get("outcome", "passed")) in ("failed", "skipped", "incomplete"):
        return None
    return record


def cache_key(
    diff_hash: str,
    *,
    model: str,
    strict: bool,
    show_nits: bool,
    profile_hash: str,
    conventions_hash: str = "",
) -> str:
    """Salted cache key: diff plus everything that changes review output.

    A bare diff hash goes stale when the model, rubric toggles, profile, or
    convention files change — the salt keeps cached findings from outliving
    the settings that produced them.
    """
    salt = json.dumps(
        [model, strict, show_nits, profile_hash, conventions_hash], ensure_ascii=False
    )
    digest = hashlib.sha256(f"{diff_hash}\n{salt}".encode()).hexdigest()
    return f"{diff_hash[:16]}-{digest[:16]}"


def print_history(repo_path: Path, console: Console, limit: int = 20) -> None:
    """Pretty-print recent review runs from the JSONL log."""
    records = read_records(repo_path)[-limit:]
    if not records:
        console.print("[dim]No review history for this repo yet.[/]")
        return
    table = Table(title="SpiritCuffs review history")
    table.add_column("when")
    table.add_column("source")
    table.add_column("files", justify="right")
    table.add_column("critical", justify="right")
    table.add_column("warn", justify="right")
    table.add_column("nit", justify="right")
    table.add_column("model")
    for record in records:
        findings = record.get("findings", [])
        counts = {"critical": 0, "warn": 0, "nit": 0}
        for finding in findings:
            severity = finding.get("severity")
            if severity in counts:
                counts[severity] += 1
        table.add_row(
            str(record.get("timestamp", "?")),
            str(record.get("source", "?")),
            str(len(record.get("files", []))),
            str(counts["critical"]),
            str(counts["warn"]),
            str(counts["nit"]),
            str(record.get("model", "?")),
        )
    console.print(table)
