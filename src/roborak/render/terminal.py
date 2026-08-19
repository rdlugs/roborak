"""The default human-facing output.

Shaped after CodeRabbit's CLI report: findings grouped severity-first with the
offending code in context and a committable fix underneath, then a one-line
summary the reader can act on.
"""

from __future__ import annotations

from pathlib import Path

from rich.console import Console, Group
from rich.panel import Panel
from rich.rule import Rule
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

from roborak.core.models import Finding, ReviewResult
from roborak.core.severity import KIND_LABEL, SEVERITY_STYLE, Severity

SEVERITY_ICON = {
    Severity.CRITICAL: "✖",
    Severity.MAJOR: "▲",
    Severity.MINOR: "•",
    Severity.INFO: "·",
}

CONTEXT_LINES = 3


def render(result: ReviewResult, console: Console, repo: Path) -> None:
    if result.errors:
        for error in result.errors:
            console.print(f"[bold red]error[/] {error}")

    if not result.findings:
        _render_clean(result, console)
        return

    console.print()
    for finding in result.sorted_findings():
        console.print(_finding_panel(finding, repo))
        console.print()

    _render_summary(result, console)


def _render_clean(result: ReviewResult, console: Console) -> None:
    changeset = result.changeset
    if changeset is not None and changeset.is_empty:
        console.print("[yellow]No changes to review.[/]")
        return
    reviewed = len(changeset.files) if changeset else 0
    console.print(f"\n[bold green]✓ No findings[/] across {reviewed} changed file(s).\n")
    _render_footer(result, console)


def _finding_panel(finding: Finding, repo: Path) -> Panel:
    style = SEVERITY_STYLE[finding.severity]
    icon = SEVERITY_ICON[finding.severity]

    heading = Text()
    heading.append(f"{icon} {finding.severity.value.upper()}", style=style)
    heading.append("  ")
    heading.append(KIND_LABEL[finding.kind], style="dim")
    heading.append("  ")
    heading.append(finding.category.value, style="dim italic")
    if finding.rule_id:
        heading.append(f"  [{finding.rule_id}]", style="magenta")
    if finding.source == "static" and finding.tool:
        heading.append(f"  via {finding.tool}", style="dim")

    parts: list[object] = [heading, Text(finding.body.strip())]

    snippet = _code_snippet(finding, repo)
    if snippet is not None:
        parts.insert(1, snippet)

    if finding.suggestion:
        parts.append(Text("Suggested fix", style="bold green"))
        parts.append(
            Syntax(
                finding.suggestion,
                _lexer_for(finding.file),
                theme="ansi_dark",
                line_numbers=False,
                word_wrap=True,
            )
        )

    return Panel(
        Group(*parts),
        title=Text(finding.title, style="bold"),
        subtitle=Text(finding.location, style="dim"),
        subtitle_align="left",
        border_style=style,
        padding=(0, 1),
    )


def _code_snippet(finding: Finding, repo: Path) -> Syntax | None:
    """Show the flagged lines in context, read from the working tree."""
    path = repo / finding.file
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return None

    start = max(1, finding.start_line - CONTEXT_LINES)
    end = min(len(lines), finding.end_line + CONTEXT_LINES)
    if start > len(lines):
        return None

    return Syntax(
        "\n".join(lines[start - 1 : end]),
        _lexer_for(finding.file),
        theme="ansi_dark",
        line_numbers=True,
        start_line=start,
        highlight_lines=set(range(finding.start_line, finding.end_line + 1)),
        word_wrap=False,
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
    console.print(f"\n[bold]{total} finding{'s' if total != 1 else ''}[/] posted.")
    _render_footer(result, console)


def _render_footer(result: ReviewResult, console: Console) -> None:
    if result.skipped_files:
        console.print(
            f"[dim]{len(result.skipped_files)} file(s) not reviewed (context budget): "
            f"{', '.join(result.skipped_files[:5])}"
            f"{' …' if len(result.skipped_files) > 5 else ''}[/]"
        )
    if result.model:
        console.print(f"[dim]model: {result.model}[/]")


_LEXERS = {
    "py": "python", "js": "javascript", "jsx": "jsx", "ts": "typescript", "tsx": "tsx",
    "php": "php", "go": "go", "rs": "rust", "rb": "ruby", "java": "java", "kt": "kotlin",
    "cs": "csharp", "c": "c", "h": "c", "cpp": "cpp", "sh": "bash", "sql": "sql",
    "yaml": "yaml", "yml": "yaml", "json": "json", "toml": "toml", "html": "html",
    "css": "css", "scss": "scss", "vue": "vue", "swift": "swift", "tf": "terraform",
}


def _lexer_for(path: str) -> str:
    return _LEXERS.get(path.rsplit(".", 1)[-1].lower(), "text")
