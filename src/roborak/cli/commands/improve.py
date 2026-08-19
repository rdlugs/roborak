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
    base: Annotated[str | None, typer.Option("--base", "-b", help="Base ref.")] = None,
    uncommitted: Annotated[bool, typer.Option("--uncommitted")] = False,
    model: Annotated[str | None, typer.Option("--model", "-m")] = None,
    max_findings: Annotated[int | None, typer.Option("--max-findings")] = None,
    config_path: Annotated[Path | None, typer.Option("--config")] = None,
    as_json: Annotated[bool, typer.Option("--json")] = False,
    agent: Annotated[bool, typer.Option("--agent", help="JSON for another agent.")] = False,
    prompt_only: Annotated[
        bool, typer.Option("--prompt-only", help="Instructions for a coding agent.")
    ] = False,
    fail_on: Annotated[Severity | None, typer.Option("--fail-on")] = None,
) -> None:
    """Propose concrete, committable improvements to the changed code."""
    console = Console(quiet=as_json or agent or prompt_only)
    session = shared.start(
        console,
        repo=repo,
        mr=mr,
        pr=pr,
        base=base,
        uncommitted=uncommitted,
        config_path=config_path,
        model=model,
        quiet_status=as_json or agent or prompt_only,
    )

    if max_findings:
        session.config.review.max_findings = max_findings

    with console.status(f"[dim]improving with {session.config.model}…[/]", spinner="dots"):
        result = Reviewer(config=session.config, repo=session.repo, llm=session.llm).improve(
            session.changeset
        )

    shared.emit(session, result, as_json=as_json, agent=agent, prompt_only_mode=prompt_only)
    shared.finish(result, fail_on)
