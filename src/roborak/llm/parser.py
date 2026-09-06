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


MAX_PATH_CHARS = 1024
"""How long an evidence path may be. A path is an identity the reducer matches an
entry back to its contract by, so it has to survive whole -- but the reply is still
untrusted, so it stays bounded."""


def _evidence_field(value: Any) -> str:
    return _as_str(value)[:MAX_EVIDENCE_CHARS]


def _evidence_path(value: Any) -> str:
    return _as_str(value)[:MAX_PATH_CHARS]


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
                    "file": _evidence_path(entry.get("file")),
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
                    "contract_file": _as_str(entry.get("contract_file")),
                    "file": _evidence_path(entry.get("file")),
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


INVESTIGATION_TOOLS = frozenset({"read_file", "search", "show_diff", "find_symbol"})

RESOLUTION_TOOLS = frozenset({"read_file", "search"})
"""The resolution pass asks a narrower question than the investigation does -- is
the reported defect still in the tree? -- and the two operations that answer it
are reading the file and looking for what used to be there. The history it needs
is handed to it up front rather than fetched on request, because the revisions
bounding that history are roborak's to choose and never the model's."""
"""The operations a model may ask for. An unknown name is refused rather than
guessed at, so a hallucinated tool cannot become a differently-shaped read."""

DISPOSITIONS = frozenset({"confirm", "revise", "drop"})


def parse_investigation_requests(
    text: str, *, limit: int, tools: frozenset[str] = INVESTIGATION_TOOLS
) -> list[dict[str, str]]:
    """The operations a model asked for this round, validated into flat argument maps.

    Shape only -- whether a path is inside the repository is the execution
    boundary's question, not the parser's. A malformed entry is skipped rather
    than raising, the same way a malformed finding is: one bad request in a list
    of four should cost that request and not the round.
    """
    data = load_yaml_mapping(text)
    raw = data.get("requests")
    if not isinstance(raw, list):
        return []

    requests: list[dict[str, str]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        tool = _as_str(entry.get("tool"))
        if tool not in tools:
            log.debug("skipping unknown investigation tool: %r", tool)
            continue
        arguments = {
            key: _as_argument(entry.get(key))
            for key in ("path", "pattern", "symbol", "regex", "start", "end")
            if entry.get(key) is not None
        }
        arguments["tool"] = tool
        requests.append(arguments)
        if len(requests) == limit:
            break
    return requests


def _as_argument(value: Any) -> str:
    """One request argument as text. A YAML boolean keeps its word.

    ``_as_str`` blanks a bool deliberately -- ``true`` is not a title -- but
    ``regex: true`` is the natural way to ask for a regular expression, and a
    blank there would quietly run the pattern as a fixed string.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    return _as_str(value)


def parse_investigation_decisions(text: str, *, valid_ids: set[str]) -> list[dict[str, Any]]:
    """What the model concluded about each candidate, keyed by roborak's own ids.

    A decision naming an id we did not issue is discarded: the ids exist so that a
    model cannot rename a candidate into one whose severity it prefers. Anything
    that does not name a known disposition is discarded too, because the default
    for a candidate nobody settled is to leave it exactly as it arrived.
    """
    data = load_yaml_mapping(text)
    raw = data.get("decisions")
    if not isinstance(raw, list):
        return []

    seen: set[str] = set()
    decisions: list[dict[str, Any]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        candidate = _as_str(entry.get("candidate"))
        disposition = _as_str(entry.get("disposition")).lower()
        if candidate not in valid_ids or candidate in seen:
            log.debug("discarding decision for unknown candidate: %r", candidate)
            continue
        if disposition not in DISPOSITIONS:
            continue
        seen.add(candidate)
        decision: dict[str, Any] = {
            "candidate": candidate,
            "disposition": disposition,
            "rationale": _as_str(entry.get("rationale"))[:MAX_EVIDENCE_CHARS],
        }
        if disposition == "revise":
            decision["revision"] = _coerce_revision(entry)
        elif disposition == "confirm":
            decision["revision"] = _coerce_revision(entry, evidence_only=True)
        decisions.append(decision)
    return decisions


FIX_STATES = frozenset({"fixed", "not_fixed", "inconclusive"})

MAX_RESOLUTION_COMMITS = 10
"""Commits one reply may attribute a fix to. A model naming more than this is
describing the branch rather than the fix."""


def parse_resolution_verdicts(
    text: str, *, valid_ids: set[str], known_commits: dict[str, set[str]]
) -> dict[str, dict[str, Any]]:
    """What the model concluded about each open thread, keyed by roborak's own ids.

    Two things are refused rather than coerced, because both would turn "we could
    not tell" into "we checked". A verdict naming an id we did not issue is
    discarded, so a model cannot rename one thread's evidence onto another. And a
    commit roborak did not put in front of it is discarded too: attribution is the
    part a reader will click, and a plausible-looking sha that never touched the
    file is worse than no sha at all.

    Anything absent, malformed or unrecognised simply does not appear in the
    result, and the caller reads a missing thread as ``inconclusive``.
    """
    data = load_yaml_mapping(text)
    raw = data.get("verdicts")
    if not isinstance(raw, list):
        return {}

    verdicts: dict[str, dict[str, Any]] = {}
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        thread = _as_str(entry.get("thread"))
        state = _as_str(entry.get("state")).lower()
        if thread not in valid_ids or thread in verdicts or state not in FIX_STATES:
            log.debug("discarding resolution verdict: thread=%r state=%r", thread, state)
            continue
        commits = _known_only(_as_commits(entry.get("commits")), known_commits.get(thread, set()))
        verdicts[thread] = {
            "state": state,
            "commits": commits,
            "summary": _as_str(entry.get("summary"))[:MAX_SUMMARY_CHARS],
        }
    return verdicts


MAX_SUMMARY_CHARS = 600
"""A resolution reply says what changed, not what the change was for."""


def _known_only(named: list[str], allowed: set[str]) -> list[str]:
    """Each named revision as the full sha roborak knows, dropping the rest.

    The prompt shows abbreviated shas because a full one is unreadable in a list,
    so a model quoting what it was shown is quoting a prefix. Expanding it here
    keeps the reply's links pointing at real commits, and an ambiguous prefix is
    dropped rather than guessed at.
    """
    resolved: list[str] = []
    for name in named:
        matches = [full for full in sorted(allowed) if full.startswith(name)]
        if len(matches) == 1 and matches[0] not in resolved:
            resolved.append(matches[0])
    return resolved[:MAX_RESOLUTION_COMMITS]


def _as_commits(value: Any) -> list[str]:
    """Revision names out of a YAML list, shape-checked before anything else."""
    if not isinstance(value, list):
        return []
    return [
        text for item in value if (text := _as_str(item).strip().lower()) and _SHA_RE.match(text)
    ]


_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")


def _coerce_revision(entry: dict[str, Any], *, evidence_only: bool = False) -> dict[str, Any]:
    """The fields a decision may change, run through the same coercion findings get.

    Routed through ``_as_evidence`` deliberately: a decision is a model claim like
    any other, so it cannot mint ``static_tool`` or wear a proven label with
    nothing written under it just because it arrived at a later stage.
    """
    evidence, note = _as_evidence(entry.get("evidence"), entry.get("evidence_note"))
    revision: dict[str, Any] = {"evidence": evidence, "evidence_note": note}
    if entry.get("evidence_files") is not None:
        revision["evidence_files"] = _as_evidence_files(
            entry.get("evidence_files"), flagged=_as_str(entry.get("file"))
        )
    if evidence_only:
        return revision

    if title := _as_str(entry.get("title")):
        revision["title"] = title[:200]
    if body := _as_str(entry.get("body")):
        revision["body"] = body
    if entry.get("severity") is not None:
        revision["severity"] = _as_enum(entry.get("severity"), Severity, Severity.MINOR)
    if entry.get("confidence") is not None:
        revision["confidence"] = _as_confidence(entry.get("confidence"))
    if (start := _as_int(entry.get("start_line"))) and start >= 1:
        revision["start_line"] = start
        end = _as_int(entry.get("end_line"))
        revision["end_line"] = end if end and end >= start else start
    return revision


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
