"""``roborak review`` — the main command."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from roborak.analysis.reviewer import Reviewer
from roborak.cli import shared
from roborak.cli.shared import fail
from roborak.core.models import Finding, ReviewResult
from roborak.core.severity import Severity
from roborak.publish.base import PublishReport
from roborak.publish.github import GitHubPublisher
from roborak.publish.gitlab import GitLabPublisher
from roborak.sources.base import SourceError
from roborak.sources.forge import Target
from roborak.state.store import StateStore, review_key
from roborak.static.runner import StaticRunner

log = logging.getLogger(__name__)


def review(
    repo: Annotated[
        Path | None,
        typer.Option("--dir", "-C", help="Repository to review.", show_default="cwd"),
    ] = None,
    mr: Annotated[
        str | None,
        typer.Option("--mr", help="GitLab merge request: an iid or a full URL."),
    ] = None,
    pr: Annotated[
        str | None,
        typer.Option("--pr", help="GitHub pull request: a number or a full URL."),
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
    post: Annotated[
        bool,
        typer.Option("--post", help="Publish the review back to the merge/pull request."),
    ] = False,
    no_summary: Annotated[
        bool, typer.Option("--no-summary", help="With --post, skip the overview comment.")
    ] = False,
    repost: Annotated[
        bool,
        typer.Option("--repost", help="Post findings even if a previous run already did."),
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
    as_json: Annotated[bool, typer.Option("--json", help="Print the full result as JSON.")] = False,
    agent: Annotated[
        bool, typer.Option("--agent", help="Print JSON shaped for another agent to act on.")
    ] = False,
    prompt_only: Annotated[
        bool,
        typer.Option("--prompt-only", help="Print findings as instructions for a coding agent."),
    ] = False,
    markdown_out: Annotated[
        Path | None,
        typer.Option("--markdown", help="Also write a markdown report to this path."),
    ] = None,
) -> None:
    """Review changes and report findings."""
    console = Console(quiet=as_json or agent or prompt_only)

    if post and not (mr or pr):
        fail(console, "--post needs --mr or --pr; there is nowhere to post a local review.")

    session = shared.start(
        console,
        repo=repo,
        mr=mr,
        pr=pr,
        base=base,
        committed=committed,
        uncommitted=uncommitted,
        include_untracked=include_untracked,
        config_path=config_path,
        model=model,
        no_llm=no_llm,
        quiet_status=as_json or agent or prompt_only,
    )

    config = session.config
    if severity_floor:
        config.review.severity_floor = severity_floor
    if max_findings:
        config.review.max_findings = max_findings
    if full_file:
        config.review.full_file = True
    if no_static:
        config.static.enabled = False

    static_findings: list[Finding] = []
    if config.static.enabled and session.changeset.origin == "local":
        # Static tools need the files on disk, which only the local source guarantees.
        with console.status("[dim]running static analysis…[/]", spinner="dots"):
            static_findings = StaticRunner(repo=session.repo, config=config.static).run(
                session.changeset
            )
    elif config.static.enabled:
        log.debug(
            "skipping static analysis: %s changes are not checked out", session.changeset.origin
        )

    status = f"reviewing with {config.model}…" if session.llm else "collecting findings…"
    with console.status(f"[dim]{status}[/]", spinner="dots"):
        result = Reviewer(
            config=config,
            repo=session.repo,
            llm=session.llm,
            static_findings=static_findings,
        ).review(session.changeset)

    shared.emit(
        session,
        result,
        as_json=as_json,
        agent=agent,
        prompt_only_mode=prompt_only,
        markdown_path=markdown_out,
    )

    if post and session.target is not None and session.token is not None:
        _publish(
            console,
            session.repo,
            session.target,
            session.token,
            result,
            no_summary=no_summary,
            repost=repost,
        )

    shared.finish(result, fail_on)


def _publish(
    console: Console,
    repo: Path,
    target: Target,
    token: str,
    result: ReviewResult,
    *,
    no_summary: bool,
    repost: bool,
) -> None:
    store = StateStore(repo)
    key = review_key(target.provider, target.host, target.project, target.number)
    seen = frozenset() if repost else frozenset(store.get(key).fingerprints)

    publisher_cls = GitLabPublisher if target.provider == "gitlab" else GitHubPublisher
    publisher = publisher_cls(
        target=target,
        token=token,
        post_summary=not no_summary,
        seen_fingerprints=seen,
    )

    try:
        with console.status("[dim]posting review…[/]", spinner="dots"):
            report = publisher.publish(result)
    except SourceError as exc:
        console.print(f"[bold red]could not post review[/] {exc}")
        return

    store.record(key, report.posted, result.changeset.head_sha if result.changeset else "")
    _report_publish(console, report)


def _report_publish(console: Console, report: PublishReport) -> None:
    console.print()
    if report.posted:
        console.print(f"[green]posted[/] {len(report.posted)} inline comment(s)")
    if report.skipped_duplicate:
        console.print(
            f"[dim]skipped {len(report.skipped_duplicate)} already posted "
            f"in an earlier run (use --repost to force)[/]"
        )
    if report.failed:
        console.print(f"[yellow]could not post {len(report.failed)} comment(s):[/]")
        for finding, reason in report.failed[:5]:
            console.print(f"  [dim]{finding.location}: {reason}[/]")
    if report.summary_posted:
        console.print("[green]posted[/] summary comment")
