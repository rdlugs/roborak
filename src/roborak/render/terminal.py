"""The panel view, behind ``--panels``.

Findings grouped severity-first, each in a bordered panel with the offending code
in context and a committable fix underneath, then a one-line summary the reader
can act on. The default terminal output is the report -- see
``render.rich_report`` -- and this is the older, denser view kept beside it for
the reader who wants one finding at a time.
"""

from __future__ import annotations

from pathlib import Path

from rich import box
from rich.console import Console, Group, RenderableType
from rich.panel import Panel
from rich.rule import Rule
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

from roborak.core.buckets import BUCKET_TITLE, Bucket, group
from roborak.core.models import (
    Finding,
    ImpactMap,
    ImpactStatus,
    ReviewResult,
    VerificationReport,
    VerificationStatus,
)
from roborak.core.severity import (
    CATEGORY_LABEL,
    EFFORT_LABEL,
    EVIDENCE_LABEL,
    KIND_LABEL,
    SEVERITY_LABEL,
    SEVERITY_STYLE,
    Kind,
    Severity,
)
from roborak.core.verdict import Verdict, gate_for, verdict_requested
from roborak.render import snippet
from roborak.render.lexers import lexer_for
from roborak.render.markdown import FLOW_SUMMARY

SEVERITY_ICON = {
    Severity.CRITICAL: "✖",
    Severity.MAJOR: "▲",
    Severity.MINOR: "•",
    Severity.INFO: "·",
}


def render(result: ReviewResult, console: Console, repo: Path) -> None:
    if result.errors:
        for error in result.errors:
            console.print(f"[bold red]error[/] {error}")

    changeset = result.changeset
    if changeset is None or not changeset.is_empty:
        _render_header(result, console)

    _render_verification(result.verification, console)
    _render_impact(result.impact, console)

    if not result.findings:
        _render_clean(result, console)
        return

    _render_buckets(result, console, repo)
    _render_summary(result, console)


_IMPACT_STYLE: dict[ImpactStatus, str] = {
    ImpactStatus.CONTAINED: "green",
    ImpactStatus.CONSUMERS_FOUND: "cyan",
    ImpactStatus.NO_REFERENCES_FOUND: "dim",
    ImpactStatus.UNSUPPORTED: "dim",
    ImpactStatus.LIMITED: "yellow",
    ImpactStatus.UNAVAILABLE: "yellow",
    ImpactStatus.NOT_APPLICABLE: "dim",
}


def _render_impact(impact: ImpactMap | None, console: Console) -> None:
    """The blast radius in one line, since the panel view has no room for a table.

    Printed before the findings and on a clean run alike: whether the change is
    contained is most worth saying precisely when there is nothing else to say.
    """
    if impact is None:
        return
    label = impact.status.value.replace("_", " ")
    style = _IMPACT_STYLE[impact.status]
    console.print()
    if impact.nodes:
        console.print(
            f"[{style}]blast radius: {label}[/] "
            f"[dim]— {len(impact.nodes)} boundary(s), "
            f"{impact.consumer_count} consumer(s)"
            f"{', truncated' if impact.truncated else ''}[/]",
            highlight=False,
        )
    else:
        console.print(f"[{style}]blast radius: {label}[/]", highlight=False)
    for note in impact.notes:
        console.print(f"  [dim]{note}[/]", highlight=False)


_VERIFICATION_STYLE: dict[VerificationStatus, str] = {
    VerificationStatus.PASSED: "green",
    VerificationStatus.FAILED: "red",
    VerificationStatus.TIMED_OUT: "yellow",
    VerificationStatus.ERRORED: "yellow",
    VerificationStatus.SKIPPED: "dim",
}


def _render_verification(report: VerificationReport | None, console: Console) -> None:
    """Whether the project's own checks ran, in the one line the panel view has.

    Printed on a clean run as well, and for the same reason the blast radius is:
    "no findings" and "no findings, and the tests were never started" are two
    different reviews, and only one of them is worth trusting.
    """
    if report is None:
        return
    label = report.status.value.replace("_", " ")
    console.print()
    console.print(
        f"[{_VERIFICATION_STYLE[report.status]}]verification: {label}[/]"
        + (f" [dim]— {len(report.runs)} check(s)[/]" if report.runs else ""),
        highlight=False,
    )
    for run in report.runs:
        if run.status is not VerificationStatus.PASSED:
            console.print(
                f"  [dim]{run.display_command} — {run.status.value.replace('_', ' ')}"
                f"{f': {run.note}' if run.note else ''}[/]",
                highlight=False,
            )
    for note in report.notes:
        console.print(f"  [dim]{note}[/]", highlight=False)


def _render_buckets(result: ReviewResult, console: Console, repo: Path) -> None:
    """Findings grouped the way the report groups them.

    A terminal cannot collapse a section, so the nitpicks are compressed instead:
    one line each, no snippet. The point is the same either way -- the small stuff
    must not be able to bury the two findings that matter.
    """
    grouped = group(result)
    for bucket, findings in grouped.items():
        if len(grouped) > 1:
            console.print()
            console.print(
                Rule(
                    Text(f"{BUCKET_TITLE[bucket]} ({len(findings)})", style="bold"),
                    align="left",
                    style="dim",
                )
            )
        if bucket is Bucket.NITPICK:
            console.print()
            for finding in findings:
                console.print(_nitpick_line(finding))
            continue

        console.print()
        for finding in findings:
            console.print(_finding_panel(finding, repo))
            console.print()


def _render_header(result: ReviewResult, console: Console) -> None:
    """What was reviewed, and what the change does.

    Mirrors ``markdown._header`` field for field: the two renderers describe the
    same review, so they must not be able to describe it differently.
    """
    walkthrough = result.walkthrough
    changeset = result.changeset

    title = (walkthrough.title if walkthrough else None) or (changeset.title if changeset else None)
    if title:
        console.print()
        console.print(Text(title, style="bold"))

    if meta := _meta_parts(result):
        if not title:
            console.print()
        console.print(Text(" · ".join(meta), style="dim"))

    if walkthrough is None:
        return

    if walkthrough.overview:
        console.print()
        console.print(Text(walkthrough.overview.strip()))

    if walkthrough.file_summaries:
        table = Table(box=box.SIMPLE, padding=(0, 1), header_style="dim")
        table.add_column("File", style="cyan", overflow="fold")
        table.add_column("Change", overflow="fold")
        for summary in walkthrough.file_summaries:
            table.add_row(summary.path, " ".join(summary.summary.split()))
        console.print()
        console.print(table)

    if walkthrough.sequence_diagram:
        # The default terminal view prints the diagram as an ordinary fenced
        # block, so the panel view printing nothing was the odd one out: it
        # carried the other two walkthrough sections and silently dropped this
        # one. Shares FLOW_SUMMARY with the report for the same reason the rest
        # of this header does -- one label, so the views cannot disagree.
        console.print()
        console.print(Text(FLOW_SUMMARY, style="bold"))
        console.print()
        console.print(Text(walkthrough.sequence_diagram.strip(), style="dim"))


def _meta_parts(result: ReviewResult) -> list[str]:
    parts: list[str] = []
    changeset = result.changeset
    if changeset is not None:
        if changeset.head_ref and changeset.base_ref:
            parts.append(f"{changeset.head_ref} → {changeset.base_ref}")
        parts.append(f"{len(changeset.files)} file(s) changed")
        if added := changeset.total_added_lines:
            parts.append(f"+{added} line(s)")
    if result.walkthrough and result.walkthrough.estimated_effort:
        parts.append(f"review effort {result.walkthrough.estimated_effort}/5")
    return parts


def _render_clean(result: ReviewResult, console: Console) -> None:
    changeset = result.changeset
    if changeset is not None and changeset.is_empty:
        console.print("[yellow]No changes to review.[/]")
        return
    reviewed = len(changeset.files) if changeset else 0
    console.print(f"\n[bold green]✓ No findings[/] across {reviewed} changed file(s).\n")
    _render_footer(result, console)


def _nitpick_line(finding: Finding) -> Text:
    """A nitpick in one line: where, what, and nothing else."""
    line = Text("  • ", style="dim")
    line.append(finding.location, style="cyan")
    line.append("  ")
    line.append(finding.title.rstrip("."), style="none")
    line.append(f"  {EFFORT_LABEL[finding.effort]}", style="dim")
    return line


def _finding_panel(finding: Finding, repo: Path) -> Panel:
    style = SEVERITY_STYLE[finding.severity]
    icon = SEVERITY_ICON[finding.severity]

    heading = Text()
    heading.append(f"{icon} ", style=style)
    heading.append(CATEGORY_LABEL[finding.category])
    heading.append(" │ ", style="dim")
    heading.append(SEVERITY_LABEL[finding.severity], style=style)
    heading.append(" │ ", style="dim")
    heading.append(EFFORT_LABEL[finding.effort])

    tail = Text()
    tail.append(KIND_LABEL[finding.kind], style="dim")
    if finding.source == "llm":
        tail.append(f"  confidence {finding.confidence:.0%}", style="dim")
        tail.append(f"  evidence {EVIDENCE_LABEL[finding.evidence].lower()}", style="dim")
    if finding.rule_id:
        tail.append(f"  [{finding.rule_id}]", style="magenta")
    if finding.source == "static" and finding.tool:
        tail.append(f"  via {finding.tool}", style="dim")

    parts: list[RenderableType] = [heading, tail]

    code = None if finding.kind is Kind.REQUIREMENT_GAP else snippet.for_finding(finding, repo)
    if code is not None:
        parts += [Text(""), code]

    parts += [Text(""), Text(finding.body.strip())]

    # The tail says which kind of evidence; this says what it actually is. A
    # sentence needs its own line, so it sits under the body rather than in the
    # badge row, matching where Markdown puts it.
    if finding.evidence_note:
        parts += [
            Text(""),
            Text(
                f"Evidence ({EVIDENCE_LABEL[finding.evidence].lower()}): {finding.evidence_note}",
                style="dim italic",
            ),
        ]

    # Where else to look. The panel is titled with the flagged file, so these are
    # the only paths in it a reader has not already been given.
    if finding.evidence_files:
        parts.append(Text("Evidence in: " + ", ".join(finding.evidence_files), style="dim italic"))

    if finding.suggestion:
        parts.append(Text(""))
        parts.append(Text("Suggested fix", style="bold green"))
        parts.append(
            Syntax(
                finding.suggestion,
                lexer_for(finding.file),
                theme="ansi_dark",
                line_numbers=False,
                word_wrap=True,
            )
        )

    where = finding.file if finding.kind is Kind.REQUIREMENT_GAP else finding.location

    return Panel(
        Group(*parts),
        title=Text(finding.title, style="bold"),
        subtitle=Text(where, style="dim"),
        subtitle_align="left",
        border_style=style,
        padding=(0, 1),
    )


def _render_summary(result: ReviewResult, console: Console) -> None:
    console.print(Rule(style="dim"))
    counts = result.counts_by_severity

    table = Table.grid(padding=(0, 2))
    table.add_column(justify="right")
    table.add_column()
    for severity in Severity:
        if counts[severity]:
            table.add_row(
                Text(str(counts[severity]), style=SEVERITY_STYLE[severity]),
                Text(severity.value, style=SEVERITY_STYLE[severity]),
            )
    console.print(table)

    total = len(result.findings)
    console.print(f"\n[bold]{total} finding{'s' if total != 1 else ''}[/].")
    _render_footer(result, console)


VERDICT_STYLE: dict[Verdict, tuple[str, str]] = {
    Verdict.PASS: ("✓ pre-merge check: pass", "bold green"),
    Verdict.BLOCKED: ("⛔ pre-merge check: blocked", "bold red"),
    Verdict.ERROR: ("⚠ pre-merge check: inconclusive", "bold yellow"),
}


def _render_verdict(result: ReviewResult, console: Console) -> None:
    """The same verdict the report states, in the view that bypasses the report.

    ``--panels`` does not go through ``render.markdown``, so the one section a
    skimming reader must not miss has to be printed again here rather than
    inherited. ``core.verdict`` is what keeps the two from drifting apart.
    """
    if not verdict_requested(result):
        return
    gate = gate_for(result)
    label, style = VERDICT_STYLE[gate.verdict]
    console.print(f"[{style}]{label}[/] [dim]{gate.summary_line()}[/]")
    source = "--fail-on" if gate.explicit else "review.block_on"
    console.print(f"[dim]floor: {gate.floor} (from {source}) · {gate.counts_line()}[/]")
    if not gate.explicit:
        console.print(f"[dim]pass --fail-on {gate.floor} to gate the exit code on this[/]")


def _render_footer(result: ReviewResult, console: Console) -> None:
    _render_verdict(result, console)
    if result.issue is not None:
        issue = result.issue
        label = f"{issue.reference} — {issue.title}" if issue.title else issue.reference
        console.print(f"[dim]reviewed against issue {label}[/]")
    if result.skipped_files:
        console.print(
            f"[dim]{len(result.skipped_files)} file(s) not reviewed (context budget): "
            f"{', '.join(result.skipped_files[:5])}"
            f"{' …' if len(result.skipped_files) > 5 else ''}[/]"
        )
    if result.review_plan is not None:
        omitted = ", ".join(
            f"{role.value} {count}" for role, count in result.review_plan.omitted_roles.items()
        )
        suffix = f" · omitted by role: {omitted}" if omitted else ""
        console.print(
            f"[dim]semantic review order: {result.review_plan.chunks} pass(es){suffix}[/]"
        )
    console.print("[dim]🤖 reviewed by roborak[/]")
