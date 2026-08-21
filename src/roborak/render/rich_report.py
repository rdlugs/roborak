"""The report, for a reader.

``markdown.render(form=Form.TERMINAL)`` writes the document; this renders it.
The translation is deliberately thin -- rich does the markdown, and this only
overrides the two things it cannot know about:

* **headings**, because rich styles them all alike, and here they carry
  structure a reader is scanning: a bucket, a file, a finding's severity;
* **the context fence**, because ``markdown`` can only put the lines in a fenced
  block, and what a reader wants is a gutter with the flagged lines lit.

Nothing here decides *what* the review says. If a finding is missing from the
screen it is missing from the document, and the document is the one every other
surface publishes.
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from markdown_it.token import Token
from rich.console import Console, ConsoleOptions, RenderResult
from rich.markdown import CodeBlock, Heading, Markdown, MarkdownElement
from rich.rule import Rule

from roborak.core.models import ReviewResult
from roborak.core.severity import SEVERITY_LABEL, SEVERITY_STYLE, Severity
from roborak.render import markdown, snippet

_SEVERITY_BY_LABEL = {label: severity for severity, label in SEVERITY_LABEL.items()}
"""The finding lead names its severity in words; this reads it back off the line
so the colour and the label cannot disagree about which finding is critical."""


def print_report(
    result: ReviewResult, repo: Path, console: Console | None = None, *, full: bool = False
) -> None:
    """Render the review to stdout, the way a person wants to read it."""
    document = markdown.render(result, form=markdown.Form.TERMINAL, repo=repo, full=full)
    (console or Console()).print(ReportMarkdown(document))


class _Heading(Heading):
    """A heading that says which kind of heading it is.

    rich centres ``h1`` and otherwise leaves every level looking the same, which
    loses the only thing the terminal form uses headings for. Levels here are
    fixed by ``markdown``: 2 is a bucket, 3 is a file, 5 is a finding's badges.
    """

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        text = self.text.copy()
        text.justify = "left"

        if self.tag == "h2":
            # The bucket divider, the shape the panel view already uses.
            text.stylize("bold")
            yield Rule(text, align="left", style="dim")
            return

        if self.tag == "h3":
            text.stylize("bold cyan")
        elif self.tag == "h5":
            if severity := _severity_of(text.plain):
                text.stylize(SEVERITY_STYLE[severity])
        else:
            text.stylize("bold")

        yield text


def _severity_of(line: str) -> Severity | None:
    return next(
        (severity for label, severity in _SEVERITY_BY_LABEL.items() if label in line),
        None,
    )


class _Fence(CodeBlock):
    """A code block, plus the one fence ``markdown`` invents.

    ``markdown.CONTEXT_FENCE`` carries a finding's surrounding lines and where
    they came from; everything else is an ordinary fenced block and is left to
    rich.
    """

    @classmethod
    def create(cls, markdown_: Markdown, token: Token) -> CodeBlock:
        info = (token.info or "").strip()
        if info.startswith(markdown.CONTEXT_FENCE):
            return _ContextBlock(info)
        return super().create(markdown_, token)


class _ContextBlock(CodeBlock):
    """The flagged lines, gutter-numbered from their real position."""

    def __init__(self, info: str) -> None:
        _, start, first, last, path = info.split(" ", 4)
        self.path = path
        self.start = int(start)
        self.first = int(first)
        self.last = int(last)
        super().__init__(lexer_name="text", theme="ansi_dark")

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        yield snippet.syntax(
            str(self.text).rstrip("\n"),
            path=self.path,
            start_line=self.start,
            highlight_from=self.first,
            highlight_to=self.last,
        )


class ReportMarkdown(Markdown):
    """``rich.Markdown``, with the report's headings and context blocks."""

    elements: ClassVar[dict[str, type[MarkdownElement]]] = {
        **Markdown.elements,
        "heading_open": _Heading,
        "fence": _Fence,
    }


__all__ = ["ReportMarkdown", "print_report"]
