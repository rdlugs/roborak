"""Turn a model's YAML reply into typed findings.

We ask for YAML rather than JSON because it survives LLM formatting quirks far
better -- unescaped quotes and multi-line code blocks are where JSON replies
break -- but a reply is still untrusted input, so every field is coerced and a
single malformed finding never discards the rest of the review.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import yaml

from roborak.core.models import Finding, Walkthrough
from roborak.core.severity import Category, Effort, Evidence, Kind, Severity

log = logging.getLogger(__name__)

_FENCE_RE = re.compile(r"^\s*```(?:ya?ml|json)?\s*\n(.*?)\n\s*```\s*$", re.DOTALL)


class ParseError(ValueError):
    """The reply could not be read as the expected structure at all."""


def strip_fences(text: str) -> str:
    """Remove a wrapping code fence, which models add despite being asked not to."""
    text = text.strip()
    match = _FENCE_RE.match(text)
    if match:
        return match.group(1)
    if text.startswith("```"):
        body = text.split("\n", 1)[1] if "\n" in text else ""
        return body.removesuffix("```").rstrip()
    return text


def load_yaml_mapping(text: str) -> dict[str, Any]:
    """Parse a reply into a mapping, retrying on the largest parseable prefix.

    A truncated reply -- the model hitting its token ceiling mid-finding -- is
    common enough to be worth recovering from rather than discarding.
    """
    cleaned = strip_fences(text)
    try:
        data = yaml.safe_load(cleaned)
    except yaml.YAMLError:
        data = _salvage_prefix(cleaned)

    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ParseError(f"Expected a YAML mapping, got {type(data).__name__}.")
    return data


def _salvage_prefix(text: str) -> Any:
    """Drop trailing lines until the remainder parses, keeping whole findings."""
    lines = text.splitlines()
    for cut in range(len(lines) - 1, 0, -1):
        if not lines[cut].lstrip().startswith("- "):
            continue
        try:
            data = yaml.safe_load("\n".join(lines[:cut]))
        except yaml.YAMLError:
            continue
        if isinstance(data, dict):
            log.warning("Recovered a truncated model reply by dropping %d lines.", len(lines) - cut)
            return data
    return None


def parse_findings(text: str, *, valid_files: set[str] | None = None) -> list[Finding]:
    """Extract findings, skipping any entry that cannot be made sense of."""
    data = load_yaml_mapping(text)
    raw = data.get("findings") or []
    if not isinstance(raw, list):
        raise ParseError("`findings` must be a list.")

    findings: list[Finding] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        finding = _coerce_finding(entry, valid_files)
        if finding is not None:
            findings.append(finding)
    return findings


MAX_EVIDENCE_ENTRIES = 20
"""How many evidence entries one pass may contribute to reconciliation. Evidence is
a pointer to somewhere the reducer should look, not the reasoning itself; a pass
that offers more than this is listing its diff rather than the handful of places
that cross a chunk boundary."""

MAX_EVIDENCE_CHARS = 300
"""How long one evidence field may be, matching the cap on a finding's evidence
note. Every pass contributes, so an unbounded field is an unbounded prompt."""


def _evidence_field(value: Any) -> str:
    return _as_str(value)[:MAX_EVIDENCE_CHARS]


def parse_requirement_evidence(text: str) -> list[dict[str, str]]:
    """Read optional map-stage evidence from a chunked review response."""
    data = load_yaml_mapping(text)
    raw = data.get("requirement_evidence") or []
    if not isinstance(raw, list):
        return []
    evidence: list[dict[str, str]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        requirement = _evidence_field(entry.get("requirement"))
        explanation = _evidence_field(entry.get("evidence"))
        if requirement and explanation:
            evidence.append(
                {
                    "requirement": requirement,
                    "file": _evidence_field(entry.get("file")),
                    "evidence": explanation,
                }
            )
        if len(evidence) == MAX_EVIDENCE_ENTRIES:
            break
    return evidence


def parse_compatibility_evidence(text: str) -> list[dict[str, str]]:
    """Read bounded cross-chunk contract evidence from a review response."""
    data = load_yaml_mapping(text)
    raw = data.get("compatibility_evidence") or []
    if not isinstance(raw, list):
        return []
    evidence: list[dict[str, str]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        contract = _evidence_field(entry.get("contract"))
        explanation = _evidence_field(entry.get("evidence"))
        if contract and explanation:
            evidence.append(
                {
                    "contract": contract,
                    # A contract name is unique only within its file, and the reducer
                    # matches evidence back to the catalog entry it came from.
                    "contract_file": _evidence_field(entry.get("contract_file")),
                    "file": _evidence_field(entry.get("file")),
                    "status": _evidence_field(entry.get("status")) or "unknown",
                    "evidence": explanation,
                }
            )
        if len(evidence) == MAX_EVIDENCE_ENTRIES:
            break
    return evidence


def _coerce_finding(entry: dict[str, Any], valid_files: set[str] | None) -> Finding | None:
    path = _as_str(entry.get("file"))
    if not path:
        return None
    if valid_files is not None and path not in valid_files:
        log.debug("Dropping finding for unknown file %r", path)
        return None

    start = _as_int(entry.get("start_line"))
    if start is None or start < 1:
        return None
    end = _as_int(entry.get("end_line")) or start

    body = _as_str(entry.get("body")) or _as_str(entry.get("description")) or ""
    title = _as_str(entry.get("title")) or _first_sentence(body) or "Review comment"
    if not body:
        return None

    evidence, note = _as_evidence(entry.get("evidence"), entry.get("evidence_note"))

    return Finding(
        file=path,
        start_line=start,
        end_line=max(end, start),
        severity=_as_enum(entry.get("severity"), Severity, Severity.MINOR),
        category=_as_enum(entry.get("category"), Category, Category.MAINTAINABILITY),
        kind=_as_enum(entry.get("kind"), Kind, Kind.POTENTIAL_ISSUE),
        effort=_as_enum(entry.get("effort"), Effort, Effort.MODERATE),
        title=title.strip()[:120],
        body=body.strip(),
        suggestion=_clean_suggestion(entry.get("suggestion")),
        rule_id=_as_str(entry.get("rule_id")) or None,
        confidence=_as_confidence(entry.get("confidence")),
        source="llm",
        evidence=evidence,
        evidence_note=note,
        evidence_files=_as_evidence_files(entry.get("evidence_files"), flagged=path),
    )


def parse_walkthrough(text: str) -> Walkthrough:
    """Read the ``describe`` reply."""
    data = load_yaml_mapping(text)
    summaries = []
    for entry in data.get("file_summaries") or []:
        if isinstance(entry, dict) and (path := _as_str(entry.get("path"))):
            summaries.append({"path": path, "summary": _as_str(entry.get("summary")) or ""})

    effort = _as_int(data.get("estimated_effort"))
    flow_diagram = data.get("flow_diagram") or data.get("sequence_diagram")
    return Walkthrough(
        title=_as_str(data.get("title")) or None,
        overview=_as_str(data.get("overview")) or "",
        file_summaries=summaries,  # type: ignore[arg-type]
        sequence_diagram=_clean_mermaid(flow_diagram),
        labels=[s for s in (_as_str(x) for x in data.get("labels") or []) if s],
        estimated_effort=effort if effort and 1 <= effort <= 5 else None,
    )


def _as_str(value: Any) -> str:
    if value is None or isinstance(value, bool):
        return ""
    return str(value).strip()


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        match = re.search(r"-?\d+", str(value))
        return int(match.group()) if match else None
    except (TypeError, ValueError):
        return None


def _as_enum[T](value: Any, enum_cls: type[T], default: T) -> T:
    raw = _as_str(value).lower().replace(" ", "_").replace("-", "_")
    try:
        return enum_cls(raw)  # type: ignore[call-arg]
    except ValueError:
        return default


def _as_confidence(value: Any) -> float:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.8
    if number > 1.0:
        number /= 100.0
    return min(max(number, 0.0), 1.0)


def _as_evidence(kind: Any, note: Any) -> tuple[Evidence, str]:
    """Read the evidence pair, refusing a label that carries nothing behind it.

    A model that writes ``evidence: execution_path`` and then says nothing has not
    shown a path, it has picked a word. Unknown labels fall back the same way, so
    the only route to a proven value is naming one *and* explaining it -- and
    ``static_tool`` is not on that route at all, being reserved for analysers.
    """
    described = _as_str(note)[:300]
    claimed = _as_enum(kind, Evidence, Evidence.UNVERIFIED)
    # ``static_tool`` means an analyser ran and said so. Nothing ran here, so a
    # model claiming it is describing the world rather than the diff -- the one
    # label no sentence can earn. The note is kept; only the claim is refused.
    if claimed is Evidence.STATIC_TOOL:
        return Evidence.UNVERIFIED, described
    if claimed.proven and not described:
        return Evidence.UNVERIFIED, ""
    return claimed, described


MAX_EVIDENCE_FILES = 5
"""How many paths a note may point at. A finding whose evidence spans six files is
describing the change rather than a defect, and the list stops being readable long
before it stops being long."""


def _as_evidence_files(value: Any, *, flagged: str) -> list[str]:
    """The other files the evidence rests on, in the order the model gave them.

    The flagged file is dropped: it is already the finding's own location, and a
    reader who sees it repeated under "Files" reasonably expects a second place to
    look. Duplicates go the same way, and anything that is not a path-shaped string
    never arrives.
    """
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    kept: list[str] = []
    for entry in value:
        path = _as_str(entry)
        if not path or path == flagged or path in kept:
            continue
        kept.append(path)
        if len(kept) == MAX_EVIDENCE_FILES:
            break
    return kept


def _clean_suggestion(value: Any) -> str | None:
    text = value if isinstance(value, str) else _as_str(value)
    if not text.strip():
        return None
    text = strip_fences(text)
    lines = text.split("\n")
    if lines and all(ln.startswith(("+", " ")) or not ln for ln in lines):
        lines = [ln[1:] if ln.startswith(("+", " ")) else ln for ln in lines]
    return "\n".join(lines).rstrip() or None


def _clean_mermaid(value: Any) -> str | None:
    text = _as_str(value)
    if not text:
        return None
    text = strip_fences(text)
    return text.removeprefix("mermaid\n").strip() or None


def _first_sentence(text: str) -> str:
    return text.strip().split(".", 1)[0][:120]
