"""Diff-hash cache + JSONL history behavior."""

from __future__ import annotations

import json
from pathlib import Path

from spc.diff import DiffResult, diff_hash, pathhash8
from spc.log import (
    append_record,
    history_file,
    last_record_for_hash,
    read_records,
)


def _diff(files: list[str], text: str = "diff --git\n") -> DiffResult:
    return DiffResult(text=text, source="upstream", files=files)


def test_history_file_slug_collision_safe(tmp_path: Path) -> None:
    a = tmp_path / "alpha"
    b = tmp_path / "nested" / "alpha"
    b.mkdir(parents=True)
    a.mkdir()
    file_a = history_file(a)
    file_b = history_file(b)
    assert file_a.name.startswith("alpha-")
    assert file_b.name.startswith("alpha-")
    assert file_a != file_b  # pathhash8 disambiguates same-named repos
    assert file_a.name.endswith(f"-{pathhash8(a)}.jsonl")


def test_append_and_read_roundtrip(tmp_path: Path) -> None:
    record_path = append_record(
        tmp_path,
        diff_hash="abc123",
        diff=_diff(["a.py"]),
        findings=[{"severity": "warn", "file": "a.py", "line": 1, "message": "m", "suggestion": ""}],
        model="test-model",
    )
    assert record_path.is_file()
    records = read_records(tmp_path)
    assert len(records) == 1
    assert records[0]["diff_hash"] == "abc123"
    assert records[0]["model"] == "test-model"
    assert records[0]["cached"] is False
    # One JSON object per line:
    lines = record_path.read_text(encoding="utf-8").strip().splitlines()
    assert json.loads(lines[0])["files"] == ["a.py"]


def test_last_record_for_hash_hits_latest(tmp_path: Path) -> None:
    append_record(tmp_path, diff_hash="old", diff=_diff(["a.py"]), findings=[], model="m1")
    append_record(tmp_path, diff_hash="new", diff=_diff(["b.py"]), findings=[], model="m2")
    append_record(tmp_path, diff_hash="old", diff=_diff(["a.py"]), findings=[], model="m3")

    hit = last_record_for_hash(tmp_path, "old")
    assert hit is not None
    assert hit["model"] == "m3"  # most recent match wins
    assert last_record_for_hash(tmp_path, "missing") is None


def test_cache_hit_skips_llm_in_execute_review(git_repo, monkeypatch, capsys) -> None:
    """Same diff twice => second run reprints cached findings without an LLM call."""
    from conftest import run_git

    from spc import cli, review
    from spc.config import SpcConfig

    repo = git_repo({"src/a.py": "x = 1\n"})
    (repo / "src/a.py").write_text("x = 2\n", encoding="utf-8")
    run_git(repo, "add", "src/a.py")
    monkeypatch.chdir(repo)

    calls = {"n": 0}

    def fake_complete(*args, **kwargs):  # noqa: ANN002, ANN003
        calls["n"] += 1
        return '[{"severity":"warn","file":"src/a.py","line":1,"message":"m","suggestion":"s"}]'

    monkeypatch.setattr(review.llm, "complete", fake_complete)

    config = SpcConfig()
    first = cli.execute_review(repo, config)
    assert first == 0
    assert calls["n"] == 1

    second = cli.execute_review(repo, config)
    assert second == 0
    assert calls["n"] == 1  # cache hit — no second LLM call
    out = capsys.readouterr().out
    assert "cached findings" in out
    assert "src/a.py" in out


def test_cache_disabled_reruns_llm(git_repo, monkeypatch) -> None:
    from conftest import run_git

    from spc import cli, review
    from spc.config import SpcConfig

    repo = git_repo({"src/a.py": "x = 1\n"})
    (repo / "src/a.py").write_text("x = 2\n", encoding="utf-8")
    run_git(repo, "add", "src/a.py")
    monkeypatch.chdir(repo)
    calls = {"n": 0}

    def fake_complete(*args, **kwargs):  # noqa: ANN002, ANN003
        calls["n"] += 1
        return "[]"

    monkeypatch.setattr(review.llm, "complete", fake_complete)
    plain = SpcConfig()
    config = SpcConfig(
        review=plain.review.__class__(cache_diff_hash=False),
    )
    cli.execute_review(repo, config)
    cli.execute_review(repo, config)
    assert calls["n"] == 2


def test_corrupt_lines_skipped(tmp_path: Path) -> None:
    path = history_file(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"diff_hash": "ok", "findings": []}) + "\n{broken\n",
        encoding="utf-8",
    )
    records = read_records(tmp_path)
    assert len(records) == 1
    assert records[0]["diff_hash"] == "ok"


def test_diff_hash_usage_consistent(tmp_path: Path) -> None:
    append_record(tmp_path, diff_hash=diff_hash("d1"), diff=_diff(["a"]), findings=[], model="m")
    assert last_record_for_hash(tmp_path, diff_hash("d1")) is not None
    assert last_record_for_hash(tmp_path, diff_hash("d2")) is None
