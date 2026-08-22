"""``roborak improve`` — suggestions only, every one committable."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from roborak.analysis.reviewer import Reviewer
from roborak.cli import shared
from roborak.core.severity import Severity


def improve(
    repo: Annotated[Path | None, typer.Option("--dir", "-C", help="Repository.")] = None,
    mr: Annotated[str | None, typer.Option("--mr", help="GitLab merge request.")] = None,
    pr: Annotated[str | None, typer.Option("--pr", help="GitHub pull request.")] = None,
    issue: Annotated[
        str | None, typer.Option("--issue", help="Issue this change should solve.")
    ] = None,
    base: Annotated[str | None, typer.Option("--base", "-b", help="Base ref.")] = None,
    uncommitted: Annotated[bool, typer.Option("--uncommitted")] = False,
    no_discussions: Annotated[
        bool,
        typer.Option("--no-discussions", help="Do not use existing MR/PR comments as context."),
    ] = False,
    model: Annotated[str | None, typer.Option("--model", "-m")] = None,
    max_findings: Annotated[int | None, typer.Option("--max-findings")] = None,
    config_path: Annotated[Path | None, typer.Option("--config")] = None,
    as_json: Annotated[bool, typer.Option("--json")] = False,
    agent: Annotated[bool, typer.Option("--agent", help="JSON for another agent.")] = False,
    prompt_only: Annotated[
        bool, typer.Option("--prompt-only", help="Instructions for a coding agent.")
    ] = False,
    panels: Annotated[
        bool,
        typer.Option("--panels", help="Show rich panels with code context instead of the report."),
    ] = False,
    fail_on: Annotated[Severity | None, typer.Option("--fail-on")] = None,
) -> None:
    """Propose concrete, committable improvements to the changed code."""
    console = Console(stderr=True, quiet=as_json or agent or prompt_only)
    session = shared.start(
        console,
        repo=repo,
        mr=mr,
        pr=pr,
        issue=issue,
        base=base,
        uncommitted=uncommitted,
        no_discussions=no_discussions,
        config_path=config_path,
        model=model,
        quiet_status=as_json or agent or prompt_only,
    )

    if max_findings:
        session.config.review.max_findings = max_findings

    with console.status(f"[dim]improving with {session.config.model}…[/]", spinner="dots"):
        result = Reviewer(
            config=session.config,
            repo=session.repo,
            llm=session.llm,
            issue=session.issue,
        ).improve(session.changeset)

    if panels:
        session.config.output.panels = True
    shared.emit(
        session,
        result,
        as_json=as_json,
        agent=agent,
        prompt_only_mode=prompt_only,
        panels=session.config.output.panels,
    )
    shared.finish(result, fail_on)
