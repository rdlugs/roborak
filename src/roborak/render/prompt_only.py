"""Findings as instructions for another agent to act on.

CodeRabbit calls this ``--prompt-only``: plain text, one problem statement per
finding, with everything needed to make the fix and nothing else. The output is
meant to be piped straight into a coding agent.
"""

from __future__ import annotations

from roborak.core.models import Finding, ReviewResult
from roborak.core.severity import Severity


def render(result: ReviewResult) -> str:
    if not result.findings:
        return "No findings."

    blocks: list[str] = []
    for index, finding in enumerate(result.sorted_findings(), start=1):
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
    instruction = (
        "Fix each finding below. Line numbers refer to the current file contents."
        if not result.has_blocking
        else "Fix each finding below, starting with the critical ones. "
        "Line numbers refer to the current file contents."
    )
    return f"{instruction}\n\nFound {header}.\n\n" + "\n\n".join(blocks)


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
