"""Findings as instructions for another agent to act on.

``--prompt-only`` emits plain text, one problem statement per finding, with
everything needed to make the fix and nothing else. The output is meant to be
piped straight into a coding agent.
"""

from __future__ import annotations

from roborak.core.models import Finding, ReviewResult
from roborak.core.severity import Severity


def render(result: ReviewResult) -> str:
    inline = result.sorted_findings()
    scanner = result.supply_chain.scanner_findings if result.supply_chain else []
    if not inline and not scanner:
        return "No findings."

    blocks: list[str] = []
    for index, finding in enumerate(inline, start=1):
        lines = [
            f"{index}. {finding.file}:{finding.start_line}"
            + (f"-{finding.end_line}" if finding.end_line != finding.start_line else ""),
            f"   severity: {finding.severity.value} ({finding.category.value})",
            f"   problem: {finding.title}",
            f"   detail: {_flatten(finding.body)}",
        ]
        if finding.suggestion:
            lines.append("   fix: replace those lines with:")
            lines += [f"     {line}" for line in finding.suggestion.splitlines()]
        blocks.append("\n".join(lines))

    counts = result.counts_by_severity
    header = ", ".join(f"{n} {s.value}" for s, n in counts.items() if n)
    found = f"Found {header}."
    if inline:
        found += " The findings below are ordered most severe first."

    sections = [AGENT_PREAMBLE, found]
    if blocks:
        sections.append("\n\n".join(blocks))
    if scanner:
        sections.append(_scanner_block(scanner))
    return "\n\n".join(sections)


def _scanner_block(findings: list[Finding]) -> str:
    """Scanner facts have a file but deliberately no invented line anchor.

    They name a whole asset, not a line, so there is nothing to "replace"; the
    agent confirms each against the current dependencies rather than editing a
    reported span.
    """
    lines = [
        "Scanner findings below name a whole asset, not a line, and have no "
        "committable line-level fix. Confirm each still applies to the current "
        "dependencies before acting."
    ]
    for finding in findings:
        identifier = f" [{finding.rule_id}]" if finding.rule_id else ""
        lines.append(
            f"- {finding.file}: {finding.severity.value} "
            f"({finding.category.value}) {finding.title}{identifier}"
        )
        lines.append(f"  detail: {_flatten(finding.body)}")
    return "\n".join(lines)


AGENT_PREAMBLE = (
    "Verify each finding against current code. Fix only still-valid issues, skip the\n"
    "rest with a brief reason, keep changes minimal, and validate."
)
"""What a coding agent needs told before it acts on someone else's review.

The review it is holding may be minutes or days old, so the first instruction is
always to check rather than to trust.
"""


def agent_instruction(finding: Finding) -> str:
    """One finding as an imperative a coding agent can act on.

    Shared with the markdown renderer's per-finding prompt blocks, so the wording
    an agent reads is the same whether it came from ``--prompt-only`` or from the
    review comment posted on the merge request.
    """
    where = f"line {finding.start_line}"
    if finding.end_line != finding.start_line:
        where = f"lines {finding.start_line}-{finding.end_line}"

    return f"In `@{finding.file}` at {where}, {agent_instruction_body(finding)}"


def agent_instruction_body(finding: Finding) -> str:
    """The imperative alone, for callers that state the location themselves."""
    body = _flatten(finding.body)
    if finding.suggestion:
        body += " A concrete replacement is offered in the review comment."
    return body


def _flatten(text: str) -> str:
    return " ".join(text.split())


def exit_severity(result: ReviewResult) -> Severity | None:
    return max((f.severity for f in result.findings), key=lambda s: s.rank, default=None)
