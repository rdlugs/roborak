"""roborak's command-line entry point."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import typer
from rich.console import Console

from roborak.cli.commands import ask as ask_cmd
from roborak.cli.commands import describe as describe_cmd
from roborak.cli.commands import improve as improve_cmd
from roborak.cli.commands import review as review_cmd

app = typer.Typer(
    name="roborak",
    help="AI code review from the terminal — local diffs, GitLab MRs, and GitHub PRs.",
    no_args_is_help=False,
    add_completion=False,
    rich_markup_mode="rich",
)

app.command("review")(review_cmd.review)
app.command("describe")(describe_cmd.describe)
app.command("improve")(improve_cmd.improve)
app.command("ask")(ask_cmd.ask)

console = Console(stderr=True)


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Show debug logging."),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Errors only."),
) -> None:
    """Run ``review`` when no subcommand is given, matching ``cr``'s bare invocation."""
    level = logging.DEBUG if verbose else logging.ERROR if quiet else logging.WARNING
    logging.basicConfig(
        level=level,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    if ctx.invoked_subcommand is None:
        ctx.invoke(review_cmd.review, repo=Path.cwd())


if __name__ == "__main__":
    app()
