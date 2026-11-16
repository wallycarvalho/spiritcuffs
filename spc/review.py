"""Review pipeline: prompt assembly, LLM call, defensive JSON parsing, output printing."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from rich.console import Console

from spc import llm
from spc.config import SpcConfig
from spc.conventions import ConventionFile
from spc.copilot import CopilotError
from spc.diff import DiffResult
from spc.languages import EXTENSION_LANGUAGE, LANGUAGE_HINTS
from spc.provider import resolve_provider_params

MAX_FINDINGS = 10
SEVERITY_ORDER = {"critical": 0, "warn": 1, "nit": 2}
SEVERITY_STYLES = {
    "critical": "bold red",
    "warn": "bold yellow",
    "nit": "dim",
}
MAX_CONVENTIONS_CHARS = 8000
MAX_PROFILE_CHARS = 6000

SEVERITIES_CANONICAL = {"critical", "warn", "nit"}
_SEVERITY_ALIASES = {
    "high": "critical", "error": "critical", "blocker": "critical", "bug": "critical",
    "medium": "warn", "warning": "warn", "minor": "warn", "caution": "warn",
    "low": "nit", "info": "nit", "style": "nit", "suggestion": "nit",
}


@dataclass(frozen=True)
class Finding:
    """One review finding as returned by the LLM (post-validation)."""

    severity: str
    file: str
    line: int
    message: str
    suggestion: str


@dataclass
class ReviewOutput:
    """Everything the review step produced, for printing and logging."""

    findings: list[Finding] = field(default_factory=list)
    dropped_files: list[str] = field(default_factory=list)
    secret_hits: list[str] = field(default_factory=list)
    cached: bool = False
    model: str = ""


def parse_findings(raw: str) -> list[Finding]:
    """Defensively parse the LLM's JSON output into :class:`Finding` objects.

    Accepts a JSON array (or an object wrapping one under ``findings``) and
    tolerates fenced-code wrappers. Malformed entries are skipped, never fatal.
    """
    text = raw.strip()
    fenced = re.search(r"```(?:json)?\s*(.+?)\s*```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return _rescue_json_array(text)

    items: list[object]
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict) and isinstance(data.get("findings"), list):
        items = data["findings"]
    else:
        return []

    findings: list[Finding] = []
    for item in items:
        finding = _coerce_finding(item)
        if finding is not None:
            findings.append(finding)
    return findings


def _rescue_json_array(text: str) -> list[Finding]:
    """Last-ditch recovery: grab the outermost ``[...]`` block and parse it."""
    match = re.search(r"\[.*\]", text, re.DOTALL)
    if not match:
        return []
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    findings = [f for item in data if (f := _coerce_finding(item)) is not None]
    return findings


def _coerce_finding(item: object) -> Finding | None:
    """Validate one finding dict; None when the shape is unusable."""
    if not isinstance(item, dict):
        return None
    severity_raw = item.get("severity")
    message = item.get("message")
    file_name = item.get("file")
    if not (
        isinstance(severity_raw, str)
        and isinstance(message, str)
        and isinstance(file_name, str)
    ):
        return None
    severity = severity_raw.strip().lower()
    severity = _SEVERITY_ALIASES.get(severity, severity)
    if severity not in SEVERITIES_CANONICAL:
        severity = "warn"
    line = item.get("line", 0)
    if not isinstance(line, int) or isinstance(line, bool):
        line = 0
    suggestion = item.get("suggestion", "")
    if not isinstance(suggestion, str):
        suggestion = ""
    return Finding(
        severity=severity,
        file=file_name.strip(),
        line=line,
        message=message.strip(),
        suggestion=suggestion.strip(),
    )


def rank_findings(findings: list[Finding]) -> list[Finding]:
    """Sort by severity (critical first), then file, then line."""
    return sorted(
        findings,
        key=lambda f: (SEVERITY_ORDER.get(f.severity, 1), f.file, f.line),
    )


def cap_findings(findings: list[Finding]) -> list[Finding]:
    """Hard cap at MAX_FINDINGS, keeping highest severity first."""
    return rank_findings(findings)[:MAX_FINDINGS]


def build_prompt(
    diff: DiffResult,
    profile: str | None,
    conventions: list[ConventionFile],
    config: SpcConfig,
    *,
    hotspot_paths: set[str] | None = None,
) -> str:
    """Assemble the review prompt: rubric + profile + conventions + languages + diff."""
    sections: list[str] = [_render_rubric(config)]

    if profile:
        sections.append(
            "## Repository profile (learned from this repo's history)\n"
            + profile[:MAX_PROFILE_CHARS]
        )

    if hotspot_paths:
        sections.append(
            "## Files with high historical change/bug rates (apply slightly more scrutiny, "
            "but never invent findings just because a file is a hotspot):\n"
            + "\n".join(f"- {path}" for path in sorted(hotspot_paths))
        )

    if conventions:
        rendered = "\n\n".join(
            f"### {item.name}\n{item.content}" for item in conventions
        )
        sections.append(
            "## Repo conventions (respect these)\n" + rendered[:MAX_CONVENTIONS_CHARS]
        )

    sections.append(_render_language_hints(diff.files))

    sections.append(
        "## Diff to review\n"
        f"(source: {diff.source} diff)\n"
        + diff.text
    )
    if diff.dropped_files:
        sections.append(
            "## Files dropped from this diff due to size limits\n"
            "(they were NOT reviewed):\n"
            + "\n".join(f"- {path}" for path in diff.dropped_files)
        )
    return "\n\n".join(sections)


def _render_rubric(config: SpcConfig) -> str:
    """The review rubric, tuned by config flags."""
    rubric = (
        "You are a rigorous senior code reviewer. Review the diff at the end.\n"
        "Report:\n"
        "- critical: bugs, logic errors, security issues (injection, secrets, auth gaps), "
        "data-loss risks\n"
        "- warn: missing error handling, untested risky paths, perf footguns\n"
    )
    if config.review.show_nits:
        rubric += "- nit: style/minor issues\n"
    else:
        rubric += "- Do NOT report style nits; only report substantive issues.\n"
    rubric += (
        "\nRespond with STRICT JSON only — an array of objects with keys "
        '"severity", "file", "line", "message", "suggestion". '
        "No prose outside the JSON. Empty array [] if nothing substantive.\n"
        'Example: [{"severity":"critical","file":"src/x.py","line":42,'
        '"message":"SQL built by string concat","suggestion":"use parameterized query"}]'
    )
    return rubric


def _render_language_hints(files: list[str]) -> str:
    """Per-language 'check for' hints for the languages present in the diff."""
    languages = sorted(
        {EXTENSION_LANGUAGE.get(Path(name).suffix, "") for name in files} - {""}
    )
    hints = [
        f"- {language}: {LANGUAGE_HINTS[language]}"
        for language in languages
        if language in LANGUAGE_HINTS
    ]
    if not hints:
        return ""
    return "## Language-specific watch items\n" + "\n".join(hints)


def run_review(
    diff: DiffResult,
    profile: str | None,
    conventions: list[ConventionFile],
    config: SpcConfig,
    console: Console,
    *,
    hotspot_paths: set[str] | None = None,
) -> ReviewOutput:
    """Execute the LLM review for *diff*. Never raises — LLM errors yield an empty output."""
    output = ReviewOutput(model=config.llm.model)
    prompt = build_prompt(diff, profile, conventions, config, hotspot_paths=hotspot_paths)
    try:

        base_url, api_key, extra_headers = _llm_call_params(config)
        raw = llm.complete(
            [{"role": "user", "content": prompt}],
            model=config.llm.model,
            base_url=base_url,
            api_key=api_key,
            timeout=config.llm.timeout,
            extra_headers=extra_headers or None,
        )
    except (llm.LlmError, CopilotError) as exc:
        console.print(f"[yellow]spiritcuffs:[/] LLM review failed ({exc}); continuing.")
        return output
    output.findings = cap_findings(parse_findings(raw))
    return output


def _llm_call_params(config: SpcConfig) -> tuple[str, str | None, dict[str, str]]:
    """(base_url, api_key, extra_headers) for the configured provider.

    Thin wrapper over the shared resolver (spc/provider.py) so reviews, learn
    synthesis, clustering, and the setup ping all behave identically.
    """
    return resolve_provider_params(config)
