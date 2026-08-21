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
from roborak.core.models import Finding, ReviewResult
from roborak.core.severity import (
    CATEGORY_LABEL,
    EFFORT_LABEL,
    KIND_LABEL,
    SEVERITY_LABEL,
    SEVERITY_STYLE,
    Kind,
    Severity,
)
from roborak.render import snippet
from roborak.render.lexers import lexer_for

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

    if not result.findings:
        _render_clean(result, console)
        return

    _render_buckets(result, console, repo)
    _render_summary(result, console)


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

    # Category, then severity, then what the fix will cost -- the order the
    # markdown badges use, so the two reports read the same way.
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
        # Only the model reports a calibrated confidence; a linter's 0.8 default
        # would be a number we invented.
        tail.append(f"  confidence {finding.confidence:.0%}", style="dim")
    if finding.rule_id:
        tail.append(f"  [{finding.rule_id}]", style="magenta")
    if finding.source == "static" and finding.tool:
        tail.append(f"  via {finding.tool}", style="dim")

    parts: list[RenderableType] = [heading, tail]

    # A gap has no line to show; every other finding gets its code in context.
    code = None if finding.kind is Kind.REQUIREMENT_GAP else snippet.for_finding(finding, repo)
    if code is not None:
        parts += [Text(""), code]

    parts += [Text(""), Text(finding.body.strip())]

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

    # A gap's line is nominal -- showing it would imply an anchor it does not have.
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


def _render_footer(result: ReviewResult, console: Console) -> None:
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
    console.print("[dim]🤖 reviewed by roborak[/]")
