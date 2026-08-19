"""Shared publishing concerns.

The one thing every publisher does identically: turn a ``Finding`` into markdown
carrying a committable suggestion block, in the syntax the forge understands.
"""

from __future__ import annotations

from typing import Protocol

from roborak.core.models import Finding, ReviewResult
from roborak.core.severity import KIND_LABEL, Severity

SEVERITY_BADGE = {
    Severity.CRITICAL: "🔴 **Critical**",
    Severity.MAJOR: "🟠 **Major**",
    Severity.MINOR: "🟡 **Minor**",
    Severity.INFO: "🔵 **Info**",
}


class Publisher(Protocol):
    def publish(self, result: ReviewResult) -> PublishReport: ...


class PublishReport:
    """What actually happened, so the CLI can report it honestly."""

    def __init__(self) -> None:
        self.posted: list[Finding] = []
        self.skipped_duplicate: list[Finding] = []
        self.failed: list[tuple[Finding, str]] = []
        self.summary_posted = False

    @property
    def total_attempted(self) -> int:
        return len(self.posted) + len(self.skipped_duplicate) + len(self.failed)


def finding_markdown(finding: Finding, *, suggestion_syntax: str = "suggestion") -> str:
    """Render one finding as a review comment.

    ``suggestion_syntax`` is the fenced-block language the forge renders as a
    one-click apply: both GitLab and GitHub call it ``suggestion``, but GitLab
    additionally supports a line-range spec, which the caller supplies.
    """
    parts = [
        f"{SEVERITY_BADGE[finding.severity]} · _{KIND_LABEL[finding.kind]}_ · "
        f"`{finding.category.value}`",
        "",
        f"**{finding.title}**",
        "",
        finding.body.strip(),
    ]

    if finding.suggestion:
        parts += ["", f"```{suggestion_syntax}", finding.suggestion.rstrip(), "```"]

    footer = []
    if finding.rule_id:
        footer.append(f"rule `{finding.rule_id}`")
    if finding.tool:
        footer.append(f"detected by `{finding.tool}`")
    if footer:
        parts += ["", f"<sub>{' · '.join(footer)}</sub>"]

    return "\n".join(parts)


def summary_markdown(result: ReviewResult) -> str:
    """The single overview comment, in the shape CodeRabbit's walkthrough takes."""
    counts = result.counts_by_severity
    lines = ["## roborak review", ""]

    walkthrough = result.walkthrough
    if walkthrough and walkthrough.overview:
        lines += [walkthrough.overview.strip(), ""]

    if not result.findings:
        lines.append("No findings. ✅")
    else:
        total = len(result.findings)
        lines.append(f"**{total} finding{'s' if total != 1 else ''}**")
        lines.append("")
        lines.append("| Severity | Count |")
        lines.append("| --- | --- |")
        for severity in Severity:
            if counts[severity]:
                lines.append(f"| {SEVERITY_BADGE[severity]} | {counts[severity]} |")

    if walkthrough and walkthrough.file_summaries:
        lines += ["", "### Walkthrough", "", "| File | Change |", "| --- | --- |"]
        for summary in walkthrough.file_summaries:
            lines.append(f"| `{summary.path}` | {summary.summary} |")

    if walkthrough and walkthrough.sequence_diagram:
        lines += ["", "```mermaid", walkthrough.sequence_diagram.strip(), "```"]

    if result.skipped_files:
        lines += [
            "",
            f"<sub>{len(result.skipped_files)} file(s) not reviewed (context budget): "
            f"{', '.join(f'`{p}`' for p in result.skipped_files[:10])}</sub>",
        ]

    if result.model:
        lines += ["", f"<sub>Reviewed by roborak using `{result.model}`.</sub>"]

    return "\n".join(lines)
