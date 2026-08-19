"""The markdown report.

Shaped like CodeRabbit's walkthrough comment: an overview, a per-file summary
table, a mermaid diagram when the change alters a flow worth drawing, then the
findings grouped by severity with committable suggestions.

The same renderer serves ``--markdown out.md`` and the forge summary comment, so
what you preview locally is what reviewers see.
"""

from __future__ import annotations

from roborak.core.models import Finding, ReviewResult
from roborak.core.severity import KIND_LABEL, Severity

SEVERITY_HEADING = {
    Severity.CRITICAL: "🔴 Critical",
    Severity.MAJOR: "🟠 Major",
    Severity.MINOR: "🟡 Minor",
    Severity.INFO: "🔵 Info",
}


def render(result: ReviewResult) -> str:
    sections = [_header(result)]

    if walkthrough := result.walkthrough:
        if walkthrough.overview:
            sections.append(walkthrough.overview.strip())
        if walkthrough.file_summaries:
            sections.append(_walkthrough_table(result))
        if walkthrough.sequence_diagram:
            sections.append(
                "### Flow\n\n```mermaid\n" + walkthrough.sequence_diagram.strip() + "\n```"
            )

    if result.findings:
        sections.append(_severity_table(result))
        sections.extend(_finding_sections(result))
    else:
        sections.append("No findings. ✅")

    if footer := _footer(result):
        sections.append(footer)

    return "\n\n".join(section for section in sections if section) + "\n"


def _header(result: ReviewResult) -> str:
    title = "# Code review"
    walkthrough = result.walkthrough
    if walkthrough and walkthrough.title:
        title = f"# {walkthrough.title}"
    elif result.changeset and result.changeset.title:
        title = f"# {result.changeset.title}"

    meta: list[str] = []
    changeset = result.changeset
    if changeset:
        if changeset.head_ref and changeset.base_ref:
            meta.append(f"`{changeset.head_ref}` → `{changeset.base_ref}`")
        meta.append(f"{len(changeset.files)} file(s) changed")
    if walkthrough and walkthrough.estimated_effort:
        meta.append(f"review effort {walkthrough.estimated_effort}/5")
    if walkthrough and walkthrough.labels:
        meta.append(" ".join(f"`{label}`" for label in walkthrough.labels))

    return f"{title}\n\n{' · '.join(meta)}" if meta else title


def _walkthrough_table(result: ReviewResult) -> str:
    assert result.walkthrough is not None
    rows = ["### Walkthrough", "", "| File | Change |", "| --- | --- |"]
    rows += [
        f"| `{summary.path}` | {_escape_cell(summary.summary)} |"
        for summary in result.walkthrough.file_summaries
    ]
    return "\n".join(rows)


def _severity_table(result: ReviewResult) -> str:
    counts = result.counts_by_severity
    total = len(result.findings)
    rows = [
        f"### {total} finding{'s' if total != 1 else ''}",
        "",
        "| Severity | Count |",
        "| --- | --- |",
    ]
    rows += [
        f"| {SEVERITY_HEADING[severity]} | {counts[severity]} |"
        for severity in Severity
        if counts[severity]
    ]
    return "\n".join(rows)


def _finding_sections(result: ReviewResult) -> list[str]:
    sections: list[str] = []
    current: Severity | None = None

    for finding in result.sorted_findings():
        if finding.severity is not current:
            current = finding.severity
            sections.append(f"## {SEVERITY_HEADING[current]}")
        sections.append(_finding_block(finding))

    return sections


def _finding_block(finding: Finding) -> str:
    lines = [
        f"### {finding.title}",
        "",
        f"`{finding.location}` · _{KIND_LABEL[finding.kind]}_ · "
        f"`{finding.category.value}` · effort: {finding.effort.value.replace('_', ' ')}",
        "",
        finding.body.strip(),
    ]

    if finding.suggestion:
        lines += ["", "```suggestion", finding.suggestion.rstrip(), "```"]

    provenance = []
    if finding.rule_id:
        provenance.append(f"rule `{finding.rule_id}`")
    if finding.tool:
        provenance.append(f"detected by `{finding.tool}`")
    if finding.source == "llm":
        provenance.append(f"confidence {finding.confidence:.0%}")
    if provenance:
        lines += ["", f"<sub>{' · '.join(provenance)}</sub>"]

    return "\n".join(lines)


def _footer(result: ReviewResult) -> str:
    parts: list[str] = []
    if result.skipped_files:
        listed = ", ".join(f"`{path}`" for path in result.skipped_files[:10])
        more = "…" if len(result.skipped_files) > 10 else ""
        parts.append(
            f"> {len(result.skipped_files)} file(s) were not reviewed because the diff "
            f"exceeded the context budget: {listed}{more}"
        )
    if result.errors:
        parts.append("> Errors: " + "; ".join(result.errors))
    if result.model:
        parts.append(f"<sub>Reviewed by roborak using `{result.model}`.</sub>")
    return "\n\n".join(parts)


def _escape_cell(text: str) -> str:
    """Keep a summary from breaking out of its table cell."""
    return " ".join(text.split()).replace("|", "\\|")
