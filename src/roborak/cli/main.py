"""roborak's command-line entry point."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import typer
from rich.console import Console

from roborak import __version__
from roborak.cli.commands import ask as ask_cmd
from roborak.cli.commands import describe as describe_cmd
from roborak.cli.commands import improve as improve_cmd
from roborak.cli.commands import review as review_cmd
from roborak.cli.commands import setup_cmd
from roborak.cli.commands.config_cmd import config_app
from roborak.cli.commands.rules import rules_app

app = typer.Typer(
    name="roborak",
    help="AI code review from the terminal - local diffs, GitLab MRs, and GitHub PRs.",
    no_args_is_help=False,
    add_completion=False,
    rich_markup_mode="rich",
)

app.command("setup")(setup_cmd.setup)
app.command("review")(review_cmd.review)
app.command("describe")(describe_cmd.describe)
app.command("improve")(improve_cmd.improve)
app.command("ask")(ask_cmd.ask)
app.add_typer(rules_app, name="rules")
app.add_typer(config_app, name="config")

console = Console(stderr=True)


def _version(value: bool) -> None:
    """Print the version and exit before the bare invocation falls through to ``review``."""
    if value:
        typer.echo(f"roborak {__version__}")
        raise typer.Exit()


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Show debug logging."),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Errors only."),
    version: bool = typer.Option(
        False,
        "--version",
        "-V",
        help="Show the version and exit.",
        callback=_version,
        is_eager=True,
    ),
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
