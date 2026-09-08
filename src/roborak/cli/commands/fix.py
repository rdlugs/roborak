"""Preview and apply snapshot-bound suggestions."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.prompt import Confirm

from roborak.analysis import autofix
from roborak.analysis.reviewer import Reviewer
from roborak.cli import shared
from roborak.cli.commands.setup_cmd import Aborted
from roborak.core.models import Finding


def fix(
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
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Preview without writing.")] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Apply without confirmation.")] = False,
) -> None:
    """Preview and apply committable improvements in the local Git checkout."""
    console = Console(stderr=True)
    if not dry_run and not yes and not shared.is_interactive():
        shared.fail(console, "Noninteractive fix requires --yes or --dry-run.")
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
        quiet_status=as_json,
    )
    try:
        plan = autofix.capture(session.repo, session.changeset)
    except (OSError, ValueError) as exc:
        shared.fail(console, str(exc))
    if max_findings is not None:
        session.config.review.max_findings = max_findings
    candidates: list[Finding] = []
    with console.status("[dim]generating fixes…[/]", spinner="dots"):
        result = Reviewer(
            config=session.config,
            repo=session.repo,
            llm=session.llm,
            issue=session.issue,
        ).improve(session.changeset.model_copy(deep=True), candidates=candidates)
    autofix.prepare(plan, candidates, result)
    plan.report.dry_run = dry_run
    if not dry_run and any(i.outcome == "eligible" for i in plan.report.items):
        if not yes:
            render(plan.report, console)
        try:
            confirmed = yes or Confirm.ask("Apply these fixes?", console=console, default=False)
        except (EOFError, KeyboardInterrupt):
            raise Aborted from None
        if confirmed:
            autofix.apply(plan)
        else:
            autofix.cancel(plan)
    output = Console()
    if as_json:
        output.print(
            plan.report.model_dump_json(indent=2), markup=False, highlight=False, soft_wrap=True
        )
    else:
        render(plan.report, output)
    if plan.report.errors or any(i.outcome == "failed" for i in plan.report.items):
        raise typer.Exit(shared.EXIT_ERROR)


def render(report: autofix.FixReport, console: Console) -> None:
    for patch in report.patches.values():
        console.print(patch, markup=False, highlight=False, soft_wrap=True)
    for item in report.items:
        console.print(
            f"{item.outcome}: {item.finding.location} — {item.finding.title}: {item.reason}",
            markup=False,
            highlight=False,
        )
    counts = {
        state: sum(i.outcome == state for i in report.items)
        for state in ("applied", "skipped", "failed", "eligible")
    }
    console.print(" · ".join(f"{count} {state}" for state, count in counts.items()))
    for error in report.errors:
        console.print(error, markup=False, highlight=False)
