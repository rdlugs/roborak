"""``roborak ask`` — free-text questions about a change."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.markdown import Markdown

from roborak.analysis.reviewer import Reviewer
from roborak.cli import shared
from roborak.cli.shared import EXIT_ERROR, EXIT_OK
from roborak.llm.client import LLMError


def ask(
    question: Annotated[str, typer.Argument(help="What you want to know about the change.")],
    repo: Annotated[Path | None, typer.Option("--dir", "-C", help="Repository.")] = None,
    mr: Annotated[str | None, typer.Option("--mr", help="GitLab merge request.")] = None,
    pr: Annotated[str | None, typer.Option("--pr", help="GitHub pull request.")] = None,
    issue: Annotated[
        str | None, typer.Option("--issue", help="Issue this change should solve.")
    ] = None,
    base: Annotated[str | None, typer.Option("--base", "-b", help="Base ref.")] = None,
    uncommitted: Annotated[bool, typer.Option("--uncommitted")] = False,
    model: Annotated[str | None, typer.Option("--model", "-m")] = None,
    config_path: Annotated[Path | None, typer.Option("--config")] = None,
    plain: Annotated[
        bool, typer.Option("--plain", help="Print the raw answer, unformatted.")
    ] = False,
) -> None:
    """Ask a question about the change and get an answer grounded in the diff."""
    console = Console(quiet=plain)
    session = shared.start(
        console,
        repo=repo,
        mr=mr,
        pr=pr,
        issue=issue,
        base=base,
        uncommitted=uncommitted,
        config_path=config_path,
        model=model,
        quiet_status=plain,
    )

    try:
        with console.status(f"[dim]asking {session.config.model}…[/]", spinner="dots"):
            answer = Reviewer(
                config=session.config,
                repo=session.repo,
                llm=session.llm,
                issue=session.issue,
            ).ask(session.changeset, question)
    except LLMError as exc:
        console.print(f"[bold red]error[/] {exc}")
        raise typer.Exit(EXIT_ERROR) from exc

    if plain:
        print(answer)
    else:
        console.print(Markdown(answer))
    raise typer.Exit(EXIT_OK)
