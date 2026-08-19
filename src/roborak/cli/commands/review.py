"""``roborak review`` — the main command."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from roborak.analysis.reviewer import Reviewer
from roborak.core.config import load_config
from roborak.core.models import Finding
from roborak.core.severity import Severity
from roborak.llm.client import LLMClient, missing_credentials
from roborak.render import terminal
from roborak.sources.base import SourceError
from roborak.sources.local_git import LocalGitSource, Scope
from roborak.static.runner import StaticRunner

log = logging.getLogger(__name__)

# Exit codes, so CI can gate on them.
EXIT_OK = 0
EXIT_FINDINGS = 1
EXIT_ERROR = 2


def review(
    repo: Annotated[
        Path | None,
        typer.Option("--dir", "-C", help="Repository to review.", show_default="cwd"),
    ] = None,
    base: Annotated[
        str | None,
        typer.Option("--base", "-b", help="Base ref to compare against, e.g. main."),
    ] = None,
    committed: Annotated[
        bool, typer.Option("--committed", help="Review only committed changes.")
    ] = False,
    uncommitted: Annotated[
        bool,
        typer.Option("--uncommitted", help="Review only staged and unstaged edits."),
    ] = False,
    include_untracked: Annotated[
        bool, typer.Option("--include-untracked", help="Also review untracked files.")
    ] = False,
    no_llm: Annotated[
        bool,
        typer.Option("--no-llm", help="Static analysis only; makes no model calls."),
    ] = False,
    no_static: Annotated[
        bool, typer.Option("--no-static", help="Skip static analysis; model only.")
    ] = False,
    model: Annotated[
        str | None, typer.Option("--model", "-m", help="Override the configured model.")
    ] = None,
    severity_floor: Annotated[
        Severity | None,
        typer.Option("--severity", "-s", help="Lowest severity to report."),
    ] = None,
    max_findings: Annotated[
        int | None, typer.Option("--max-findings", help="Cap the number of findings.")
    ] = None,
    full_file: Annotated[
        bool,
        typer.Option("--full-file", help="Allow findings on lines the change did not touch."),
    ] = False,
    config_path: Annotated[
        Path | None, typer.Option("--config", help="Path to a config file.")
    ] = None,
    fail_on: Annotated[
        Severity | None,
        typer.Option("--fail-on", help="Exit non-zero when a finding reaches this severity."),
    ] = None,
) -> None:
    """Review changes and report findings."""
    console = Console()
    repo = (repo or Path.cwd()).resolve()

    try:
        config = load_config(repo, config_path)
    except (OSError, ValueError) as exc:
        console.print(f"[bold red]config error[/] {exc}")
        raise typer.Exit(EXIT_ERROR) from exc

    # CLI flags are the highest-precedence config layer.
    if model:
        config.llm.model = model
    if severity_floor:
        config.review.severity_floor = severity_floor
    if max_findings:
        config.review.max_findings = max_findings
    if full_file:
        config.review.full_file = True

    scope = Scope.COMMITTED if committed else Scope.UNCOMMITTED if uncommitted else Scope.ALL
    if committed and uncommitted:
        console.print("[bold red]error[/] --committed and --uncommitted are mutually exclusive.")
        raise typer.Exit(EXIT_ERROR)

    source = LocalGitSource(
        repo=repo,
        scope=scope,
        base=base,
        include_untracked=include_untracked,
    )

    try:
        changeset = source.load()
    except SourceError as exc:
        console.print(f"[bold red]error[/] {exc}")
        raise typer.Exit(EXIT_ERROR) from exc

    if no_static:
        config.static.enabled = False

    # Credentials are checked before any work so a missing key fails in a second
    # rather than after a static pass the user then has to wait through.
    if not no_llm and (missing := missing_credentials(config.model)):
        console.print(
            f"[bold red]error[/] {config.model} needs [bold]{missing}[/] to be set.\n"
            "[dim]Set it, pick another model with --model, or run --no-llm.[/]"
        )
        raise typer.Exit(EXIT_ERROR)

    static_findings: list[Finding] = []
    if config.static.enabled:
        with console.status("[dim]running static analysis…[/]", spinner="dots"):
            static_findings = StaticRunner(repo=repo, config=config.static).run(changeset)

    llm = None if no_llm else LLMClient(config.llm)
    status = f"reviewing with {config.model}…" if llm else "collecting findings…"
    with console.status(f"[dim]{status}[/]", spinner="dots"):
        result = Reviewer(
            config=config, repo=repo, llm=llm, static_findings=static_findings
        ).review(changeset)

    terminal.render(result, console, repo)

    if result.errors:
        raise typer.Exit(EXIT_ERROR)
    if fail_on and any(f.severity.at_least(fail_on) for f in result.findings):
        raise typer.Exit(EXIT_FINDINGS)
    raise typer.Exit(EXIT_OK)
