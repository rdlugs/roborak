"""Findings as instructions for another agent to act on.

CodeRabbit calls this ``--prompt-only``: plain text, one problem statement per
finding, with everything needed to make the fix and nothing else. The output is
meant to be piped straight into a coding agent.
"""

from __future__ import annotations

from roborak.core.models import ReviewResult
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


def _flatten(text: str) -> str:
    return " ".join(text.split())


def exit_severity(result: ReviewResult) -> Severity | None:
    return max((f.severity for f in result.findings), key=lambda s: s.rank, default=None)
