"""The bounded investigation stage.

Runs between candidate generation and finding validation, and exists to serve one
gate: ``validator.enforce_evidence`` demotes a critical or major model finding
that cannot say what makes it true. Before this stage, a model had no way to go
and find out. Now it may ask for a small number of bounded reads and searches,
and then confirm, revise, or drop each candidate on what came back.

Three rules hold everywhere below, and the tests exist mostly to keep them:

- a candidate the stage cannot settle is returned exactly as it arrived. A tool
  error, an exhausted budget, an unparseable reply and a raised exception all end
  the same way, because "we could not tell" must never be recorded as "we
  checked";
- the model never names a candidate, only an id roborak issued, so it cannot
  rename one finding's decision onto another;
- everything read from the repository is untrusted input and reaches the prompt
  escaped, like every other piece of repository text.

Non-fatal by construction, the same way the walkthrough is: a review whose
investigation failed is still a review.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from roborak.analysis.validator import is_unproven_blocker
from roborak.context import ast_context
from roborak.context.diff import detect_language
from roborak.core.config import InvestigateConfig
from roborak.core.models import (
    ChangeSet,
    Finding,
    InvestigationDecision,
    InvestigationOperation,
    InvestigationOutcome,
    InvestigationReport,
    InvestigationStatus,
)
from roborak.core.severity import Evidence, Severity
from roborak.investigate import availability, tools
from roborak.llm.parser import (
    ParseError,
    parse_investigation_decisions,
    parse_investigation_requests,
)
from roborak.llm.prompt import build_investigation_prompt

log = logging.getLogger(__name__)

Complete = Callable[[str, str], str]
"""How the stage reaches a model: system and user in, reply text out. Injected so
the stage can be tested without a ``Reviewer``, and so its calls are recorded as
usage by whatever supplies it."""


@dataclass
class _Budget:
    """What is left. Every ceiling is checked before the work, never after."""

    config: InvestigateConfig
    files_opened: set[str] = field(default_factory=set)
    tokens_spent: int = 0

    def may_open(self, path: str) -> bool:
        if path in self.files_opened:
            return True
        return len(self.files_opened) < self.config.max_files

    def spent(self) -> bool:
        return self.tokens_spent >= self.config.token_budget


def select(findings: list[Finding], config: InvestigateConfig) -> list[Finding]:
    """The candidates worth a model call.

    Deliberately the findings the evidence policy is about to act on: those it
    would demote, plus any critical or major model claim. Investigating a minor
    nitpick costs the same call and changes nothing a reader would do.
    """
    candidates = [
        finding
        for finding in findings
        if finding.source == "llm"
        and (is_unproven_blocker(finding) or finding.severity.at_least(Severity.MAJOR))
    ]
    candidates.sort(key=lambda f: (-f.severity.rank, -f.confidence, f.file, f.start_line))
    return candidates[: config.max_candidates]


def investigate(
    findings: list[Finding],
    changeset: ChangeSet,
    *,
    repo: Path,
    config: InvestigateConfig,
    complete: Complete,
) -> tuple[list[Finding], InvestigationReport]:
    """Settle what can be settled; return every finding and a record of the attempt."""
    report = InvestigationReport()
    candidates = select(findings, config)
    report.candidates = len(candidates)
    if not candidates:
        report.notes.append("no candidate needed evidence that the review did not already have.")
        return findings, report

    access = availability.resolve(changeset, repo)
    report.notes.extend(access.notes)
    if not access.usable:
        report.status = InvestigationStatus.UNAVAILABLE
        report.decisions = [_unresolved(finding, index) for index, finding in enumerate(candidates)]
        return findings, report

    ids = {_candidate_id(index): finding for index, finding in enumerate(candidates)}
    budget = _Budget(config=config)
    operations: list[dict[str, object]] = []
    decisions: list[dict[str, object]] = []

    for round_index in range(1, config.max_rounds + 1):
        report.rounds = round_index
        final = round_index == config.max_rounds or budget.spent()
        prompt = build_investigation_prompt(
            candidates=[_describe(key, finding) for key, finding in ids.items()],
            operations=operations,
            limits={
                "max_requests_per_round": config.max_requests_per_round,
                "max_lines_per_read": config.max_lines_per_read,
                "max_search_results": config.max_search_results,
                "rounds_remaining": config.max_rounds - round_index,
            },
            can_search=access.searches,
            availability_notes=access.notes,
            final_round=final,
        )
        try:
            reply = complete(prompt.system, prompt.user)
        except Exception as exc:  # noqa: BLE001 - a failed stage never fails the review
            log.warning("investigation round %d failed: %s", round_index, exc)
            report.notes.append(f"round {round_index} did not complete: {exc}")
            report.status = InvestigationStatus.ERRORED
            break

        budget.tokens_spent += len(reply) // 4

        try:
            decided = parse_investigation_decisions(reply, valid_ids=set(ids))
        except ParseError as exc:
            log.debug("unparseable investigation reply: %s", exc)
            report.notes.append(f"round {round_index} returned a reply that could not be read.")
            break
        if decided:
            decisions = list(decided)
            break
        if final:
            report.notes.append("the round limit was reached before the model decided.")
            break

        try:
            requests = parse_investigation_requests(reply, limit=config.max_requests_per_round)
        except ParseError:
            requests = []
        if not requests:
            report.notes.append(f"round {round_index} asked for nothing and decided nothing.")
            break

        for request in requests:
            record = _execute(request, repo, changeset, config, access, budget)
            record.round_index = round_index
            report.operations.append(record)
            operations.append(
                {
                    "request": record.display_request,
                    "outcome": record.outcome.value,
                    "truncated": record.truncated,
                    "result": record.result or record.note,
                }
            )

    report.decisions = _apply(decisions, ids, findings, report)
    if report.status is InvestigationStatus.SKIPPED:
        report.status = _status(report)
    # A run that could only read the diff is partial however cleanly it ended: a
    # reader must not take a settled candidate here for one checked against the
    # whole repository.
    if access.status is InvestigationStatus.PARTIAL and report.status is (
        InvestigationStatus.COMPLETED
    ):
        report.status = InvestigationStatus.PARTIAL
    return findings, report


def _status(report: InvestigationReport) -> InvestigationStatus:
    """Completed only when every candidate was actually settled."""
    if not report.decisions:
        return InvestigationStatus.SKIPPED
    if report.unresolved:
        return InvestigationStatus.PARTIAL
    return InvestigationStatus.COMPLETED


def _candidate_id(index: int) -> str:
    return f"c{index + 1}"


def _describe(key: str, finding: Finding) -> dict[str, object]:
    """One candidate as the model sees it. Repository text, so it is escaped."""
    from roborak.llm.prompt import _escape_untrusted

    return {
        "id": key,
        "file": _escape_untrusted(finding.file),
        "start_line": finding.start_line,
        "end_line": finding.end_line,
        "severity": finding.severity.value,
        "kind": finding.kind.value,
        "title": _escape_untrusted(finding.title),
        "body": _escape_untrusted(finding.body),
        "evidence": finding.evidence.value,
        "would_be_demoted": is_unproven_blocker(finding),
    }


def _unresolved(finding: Finding, index: int) -> InvestigationDecision:
    return InvestigationDecision(
        candidate=_candidate_id(index),
        disposition="unresolved",
        location=finding.location,
        title=finding.title,
    )


def _execute(
    request: dict[str, str],
    repo: Path,
    changeset: ChangeSet,
    config: InvestigateConfig,
    access: availability.Availability,
    budget: _Budget,
) -> InvestigationOperation:
    """Run one validated request, or record why it did not run."""
    tool = request.pop("tool")
    operation = InvestigationOperation(tool=tool, arguments=dict(request))

    if budget.spent():
        return _refuse(operation, "the investigation token budget was already spent.")

    if tool == "show_diff":
        result = tools.show_diff(changeset, request.get("path", ""), config=config)
        return _record(operation, result)

    if tool == "search":
        if not access.searches:
            return _refuse(operation, "searching needs a checkout matching the reviewed change.")
        result = tools.search(
            repo,
            request.get("pattern", ""),
            regex=request.get("regex", "").lower() in {"true", "yes", "1"},
            path_prefix=request.get("path", ""),
            config=config,
        )
        return _record(operation, result)

    path = request.get("path", "")
    if not budget.may_open(path):
        return _refuse(operation, "the investigation file limit was reached.")

    if tool == "find_symbol":
        if not access.reads_working_tree:
            return _refuse(operation, "reading needs a checkout matching the reviewed change.")
        result = _find_symbol(repo, path, request.get("symbol", ""), config)
        budget.files_opened.add(path)
        return _record(operation, result)

    if tool == "read_file":
        if not (access.reads_working_tree or access.reads_changeset):
            return _refuse(operation, "no trustworthy source of file content was available.")
        result = _read(repo, changeset, path, request, config, access)
        budget.files_opened.add(path)
        return _record(operation, result)

    return _refuse(operation, f"unknown operation: {tool}")


def _read(
    repo: Path,
    changeset: ChangeSet,
    path: str,
    request: dict[str, str],
    config: InvestigateConfig,
    access: availability.Availability,
) -> tools.ToolResult:
    """A line range, from the working tree when it is trustworthy and the changeset when not.

    The fallback matters: a forge review against a checkout that has moved on can
    still answer questions about the files the change itself carries, because that
    content came from the forge rather than from whatever branch is checked out.
    """
    start = _as_line(request.get("start"), 1)
    end = _as_line(request.get("end"), start + config.max_lines_per_read - 1)

    if access.reads_working_tree:
        return tools.read_lines(repo, path, start=start, end=end, config=config)

    file = changeset.file_by_path(path)
    if file is None or file.new_content is None:
        return tools.ToolResult(
            error=(
                f"{path} cannot be read: the checkout does not match the reviewed change, "
                "and this file's content did not come with it."
            )
        )
    lines = file.new_content.splitlines()
    last = min(len(lines), max(start, end))
    if start > len(lines):
        return tools.ToolResult(error=f"{path} has {len(lines)} lines; {start} is past the end")
    window = lines[start - 1 : last]
    clipped = len(window) > config.max_lines_per_read
    window = window[: config.max_lines_per_read]
    numbered = "\n".join(f"{start + offset}: {line}" for offset, line in enumerate(window))
    text, cut = tools.bound(numbered, config.max_output_chars)
    return tools.ToolResult(text=text, truncated=clipped or cut)


def _find_symbol(repo: Path, path: str, symbol: str, config: InvestigateConfig) -> tools.ToolResult:
    """Where a symbol is declared in one file, when a parser can tell.

    Degrades rather than fails: tree-sitter is optional throughout roborak, and a
    stage that raised when a grammar was missing would take the review with it.
    """
    if not symbol:
        return tools.ToolResult(error="no symbol was named.")
    target = tools.resolve_in_repo(repo, path)
    if target is None or not target.is_file():
        return tools.ToolResult(error=f"path is outside the repository or not a file: {path}")
    try:
        source = target.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return tools.ToolResult(error=f"could not read {path}: {exc}")

    tree = ast_context.parse(detect_language(path), source)
    if tree is None:
        return tools.ToolResult(error=f"no parser is available for {path}.")

    lines = source.splitlines()
    found: list[str] = []
    for node in ast_context.walk(tree.root_node):
        if node.type not in ast_context.SYMBOL_TYPES:
            continue
        if ast_context.node_name(node) != symbol:
            continue
        start = node.start_point[0] + 1
        end = min(start + config.max_lines_per_read - 1, node.end_point[0] + 1)
        body = "\n".join(f"{n}: {lines[n - 1]}" for n in range(start, end + 1) if n <= len(lines))
        found.append(f"{path}:{start} ({node.type})\n{body}")
    if not found:
        return tools.ToolResult(error=f"{symbol} is not declared in {path}.")
    text, cut = tools.bound("\n\n".join(found), config.max_output_chars)
    return tools.ToolResult(text=text, truncated=cut)


def _as_line(value: str | None, default: int) -> int:
    try:
        return max(1, int(str(value)))
    except (TypeError, ValueError):
        return default


def _refuse(operation: InvestigationOperation, note: str) -> InvestigationOperation:
    operation.outcome = InvestigationOutcome.REFUSED
    operation.note = note
    return operation


def _record(operation: InvestigationOperation, result: tools.ToolResult) -> InvestigationOperation:
    from roborak.llm.prompt import _escape_untrusted

    if not result.ok:
        operation.outcome = InvestigationOutcome.ERRORED
        operation.note = result.error
        return operation
    operation.result = _escape_untrusted(result.text)
    operation.truncated = result.truncated
    operation.outcome = InvestigationOutcome.OK if result.text else InvestigationOutcome.EMPTY
    return operation


def _apply(
    decisions: list[dict[str, object]],
    ids: dict[str, Finding],
    findings: list[Finding],
    report: InvestigationReport,
) -> list[InvestigationDecision]:
    """Fold the model's conclusions into the findings list, in place.

    Anything not decided is recorded as unresolved and left alone -- which is the
    whole safety property of the stage, so it is expressed by simply not touching
    the finding rather than by a branch that could be got wrong.
    """
    settled: dict[str, dict[str, object]] = {
        str(decision["candidate"]): decision for decision in decisions
    }
    recorded: list[InvestigationDecision] = []

    for key, finding in ids.items():
        decision = settled.get(key)
        if decision is None:
            recorded.append(
                InvestigationDecision(
                    candidate=key,
                    disposition="unresolved",
                    location=finding.location,
                    title=finding.title,
                )
            )
            continue

        disposition = str(decision["disposition"])
        rationale = str(decision.get("rationale", ""))
        if disposition not in {"confirm", "revise", "drop"}:  # pragma: no cover - parser filters
            continue
        if disposition == "drop":
            if finding in findings:
                findings.remove(finding)
            recorded.append(
                InvestigationDecision(
                    candidate=key,
                    disposition="drop",
                    location=finding.location,
                    title=finding.title,
                    rationale=rationale,
                )
            )
            continue

        revision = decision.get("revision")
        if isinstance(revision, dict):
            _revise(finding, revision)
        recorded.append(
            InvestigationDecision(
                candidate=key,
                disposition="confirm" if disposition == "confirm" else "revise",
                location=finding.location,
                title=finding.title,
                rationale=rationale,
            )
        )

    if report.unresolved:
        log.debug("%d candidates were left unresolved", report.unresolved)
    return recorded


def _revise(finding: Finding, revision: dict[str, object]) -> None:
    """Write back only the fields a decision is allowed to move."""
    for attribute in (
        "title",
        "body",
        "severity",
        "confidence",
        "start_line",
        "end_line",
        "evidence",
        "evidence_note",
        "evidence_files",
    ):
        if attribute not in revision:
            continue
        value = revision[attribute]
        # A decision always carries an evidence pair, defaulted when the model said
        # nothing about it. Writing that default back would strip the evidence a
        # confirmed finding already had, and demote the finding this stage just proved.
        if (
            attribute == "evidence"
            and value is Evidence.UNVERIFIED
            and finding.evidence is not Evidence.UNVERIFIED
        ):
            continue
        if attribute == "evidence_note" and not value and finding.evidence_note:
            continue
        setattr(finding, attribute, value)
    if finding.end_line < finding.start_line:
        finding.end_line = finding.start_line
