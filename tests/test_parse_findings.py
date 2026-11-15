"""Defensive JSON parsing of LLM output."""

from __future__ import annotations

from spc.review import cap_findings, parse_findings, rank_findings


def test_parses_clean_array() -> None:
    raw = """[
      {"severity": "critical", "file": "a.py", "line": 3, "message": "m", "suggestion": "s"},
      {"severity": "warn", "file": "b.py", "line": 1, "message": "m2", "suggestion": ""}
    ]"""
    findings = parse_findings(raw)
    assert len(findings) == 2
    assert findings[0].severity == "critical"
    assert findings[0].line == 3


def test_parses_fenced_json() -> None:
    raw = "Here you go:\n```json\n[{\"severity\":\"warn\",\"file\":\"x.py\",\"line\":1,\"message\":\"m\",\"suggestion\":\"s\"}]\n```"
    findings = parse_findings(raw)
    assert len(findings) == 1
    assert findings[0].file == "x.py"


def test_parses_wrapped_object() -> None:
    raw = '{"findings": [{"severity": "nit", "file": "a", "line": 0, "message": "m", "suggestion": ""}]}'
    findings = parse_findings(raw)
    assert len(findings) == 1
    assert findings[0].severity == "nit"


def test_malformed_output_returns_empty_not_crash() -> None:
    for bad in ("", "complete garbage", "{\"a\": 1}", "[{oops", "null", "123"):
        assert parse_findings(bad) == []


def test_rescues_array_with_leading_prose() -> None:
    raw = 'Sure! The issues are: [{"severity":"warn","file":"a","line":1,"message":"m","suggestion":""}] hope that helps'
    assert len(parse_findings(raw)) == 1


def test_severity_aliases_normalized() -> None:
    raw = '[{"severity":"high","file":"a","line":1,"message":"m","suggestion":""},'
    raw += ' {"severity":"low","file":"b","line":1,"message":"m","suggestion":""}]'
    findings = parse_findings(raw)
    assert [f.severity for f in findings] == ["critical", "nit"]


def test_unknown_severity_defaults_to_warn() -> None:
    raw = '[{"severity":"banana","file":"a","line":1,"message":"m","suggestion":""}]'
    assert parse_findings(raw)[0].severity == "warn"


def test_bad_entries_skipped() -> None:
    raw = '[{"severity": 7, "file": "a"}, "not-a-dict", {"severity":"warn","file":"ok.py","line":2,"message":"m","suggestion":""}]'
    findings = parse_findings(raw)
    assert len(findings) == 1
    assert findings[0].file == "ok.py"


def test_non_int_line_coerced_to_zero() -> None:
    raw = '[{"severity":"warn","file":"a","line":"five","message":"m","suggestion":""}]'
    assert parse_findings(raw)[0].line == 0


def test_cap_keeps_ten_highest_severity() -> None:
    findings = rank_findings(
        [
            type("F", (), {"severity": s, "file": f"{i}.py", "line": 1, "message": "", "suggestion": ""})()
            for i, s in enumerate(["nit"] * 12 + ["critical"])
        ]
    )
    capped = cap_findings(findings)  # type: ignore[arg-type]
    assert len(capped) == 10
    assert capped[0].severity == "critical"
