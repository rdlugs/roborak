"""``roborak describe`` — a walkthrough of the change, not a critique."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.markdown import Markdown

from roborak.analysis.reviewer import Reviewer
from roborak.cli import shared
from roborak.cli.shared import EXIT_ERROR, EXIT_OK
from roborak.render import markdown as markdown_render


def describe(
    repo: Annotated[
        Path | None, typer.Option("--dir", "-C", help="Repository to describe.")
    ] = None,
    mr: Annotated[str | None, typer.Option("--mr", help="GitLab merge request.")] = None,
    pr: Annotated[str | None, typer.Option("--pr", help="GitHub pull request.")] = None,
    base: Annotated[str | None, typer.Option("--base", "-b", help="Base ref.")] = None,
    model: Annotated[str | None, typer.Option("--model", "-m")] = None,
    config_path: Annotated[Path | None, typer.Option("--config")] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Print JSON.")] = False,
    markdown_out: Annotated[
        Path | None, typer.Option("--markdown", help="Write the walkthrough to this path.")
    ] = None,
) -> None:
    """Summarise a change: title, overview, per-file table, and a flow diagram."""
    console = Console(quiet=as_json)
    session = shared.start(
        console,
        repo=repo,
        mr=mr,
        pr=pr,
        base=base,
        config_path=config_path,
        model=model,
        quiet_status=as_json,
    )

    with console.status(f"[dim]describing with {session.config.model}…[/]", spinner="dots"):
        result = Reviewer(config=session.config, repo=session.repo, llm=session.llm).describe(
            session.changeset
        )

    if as_json:
        shared.emit(session, result, as_json=True)
    else:
        if markdown_out is not None:
            markdown_out.write_text(markdown_render.render(result), encoding="utf-8")
        if result.walkthrough is None:
            console.print("[yellow]Nothing to describe.[/]")
        else:
            console.print(Markdown(markdown_render.render(result)))
        if markdown_out is not None:
            console.print(f"[dim]written to {markdown_out}[/]")

    raise typer.Exit(EXIT_ERROR if result.errors else EXIT_OK)
