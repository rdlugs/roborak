"""The flagged lines, read out of the working tree.

Two surfaces show a finding's code in context -- the panel view and the terminal
report -- and they must not be able to disagree about how much context that is,
or about what happens when the file is not there to read. Both live here.

Only ever the working tree: a finding is in new-file coordinates, so the file on
disk is the one it points at. A forge review has no working tree, which is why
every caller has to cope with ``None``.
"""

from __future__ import annotations

from pathlib import Path

from rich.syntax import Syntax

from roborak.core.models import Finding
from roborak.render.lexers import lexer_for

CONTEXT_LINES = 3

MAX_LINES = 14
"""How much of a finding's span is worth showing.

A static analyser will happily flag a range seventeen lines long, and pasting all
of it back plus context turns one finding into a screenful. The lead line names
the real range, so a reader who needs the rest knows exactly where to look.
"""


def window(
    repo: Path,
    path: str,
    start_line: int,
    end_line: int,
    *,
    context: int = CONTEXT_LINES,
    limit: int = MAX_LINES,
) -> tuple[str, int] | None:
    """Lines ``start_line``..``end_line`` of ``path`` plus ``context`` either side.

    The one definition of how much of a file is worth quoting back. A finding's
    code context and a blast-radius consumer snippet are the same problem -- show
    enough of the line to read it, not enough to bury the point -- and reading them
    through one function is what keeps the two from drifting apart.

    ``None`` when there is nothing honest to show: no such file, not text, or a
    range that starts past the end of it.
    """
    try:
        lines = (repo / path).read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return None

    start = max(1, start_line - context)
    end = min(len(lines), end_line + context)
    if start > len(lines):
        return None

    end = min(end, start + limit - 1)
    return "\n".join(lines[start - 1 : end]), start


def read(
    finding: Finding,
    repo: Path,
    *,
    context: int = CONTEXT_LINES,
    limit: int = MAX_LINES,
) -> tuple[str, int] | None:
    """The flagged lines plus ``context`` either side, and the first line's number.

    Cut off after ``limit`` lines: a long span is worth showing the head of, not
    all of. ``None`` when there is nothing honest to show at all -- no such file,
    not text, or a finding pointing past the end of it.
    """
    return window(
        repo,
        finding.file,
        finding.start_line,
        finding.end_line,
        context=context,
        limit=limit,
    )


def syntax(
    code: str,
    *,
    path: str,
    start_line: int,
    highlight_from: int,
    highlight_to: int,
) -> Syntax:
    """The snippet, gutter-numbered from its real position with the flagged lines lit."""
    return Syntax(
        code,
        lexer_for(path),
        theme="ansi_dark",
        line_numbers=True,
        start_line=start_line,
        highlight_lines=set(range(highlight_from, highlight_to + 1)),
        word_wrap=False,
    )


def for_finding(
    finding: Finding, repo: Path, *, context: int = CONTEXT_LINES, limit: int = MAX_LINES
) -> Syntax | None:
    """``read`` and ``syntax`` together, for the caller that wants both."""
    found = read(finding, repo, context=context, limit=limit)
    if found is None:
        return None
    code, start = found
    return syntax(
        code,
        path=finding.file,
        start_line=start,
        highlight_from=finding.start_line,
        highlight_to=finding.end_line,
    )
