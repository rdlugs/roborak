"""``roborak review`` — the main command."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from roborak.analysis.reviewer import Reviewer
from roborak.core.config import load_config
from roborak.core.models import ChangeSet, Finding, ReviewResult
from roborak.core.severity import Severity
from roborak.llm.client import LLMClient, missing_credentials
from roborak.publish.base import PublishReport
from roborak.publish.github import GitHubPublisher
from roborak.publish.gitlab import GitLabPublisher
from roborak.render import terminal
from roborak.sources.base import SourceError
from roborak.sources.forge import Provider, Target, detect_host, get_token, parse_target
from roborak.sources.github import GitHubSource
from roborak.sources.gitlab import GitLabSource
from roborak.sources.local_git import LocalGitSource, Scope
from roborak.state.store import StateStore, review_key
from roborak.static.runner import StaticRunner

log = logging.getLogger(__name__)

# Exit codes, so CI can gate on them.
EXIT_OK = 0
EXIT_FINDINGS = 1
EXIT_ERROR = 2

TOKEN_HELP = {
    "gitlab": "GITLAB_TOKEN (or ROBORAK_GITLAB_TOKEN)",
    "github": "GITHUB_TOKEN, or sign in with `gh auth login`",
}


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
) -> None:
    """Review changes and report findings."""
    console = Console()
    repo = (repo or Path.cwd()).resolve()

    if mr and pr:
        _fail(console, "--mr and --pr are mutually exclusive.")
    if committed and uncommitted:
        _fail(console, "--committed and --uncommitted are mutually exclusive.")
    if post and not (mr or pr):
        _fail(console, "--post needs --mr or --pr; there is nowhere to post a local review.")

    try:
        config = load_config(repo, config_path)
    except (OSError, ValueError) as exc:
        _fail(console, f"config error: {exc}")

    if model:
        config.llm.model = model
    if severity_floor:
        config.review.severity_floor = severity_floor
    if max_findings:
        config.review.max_findings = max_findings
    if full_file:
        config.review.full_file = True
    if no_static:
        config.static.enabled = False

    # Credentials first: a missing key should fail in a second, not after a diff
    # has been fetched and a static pass sat through.
    if not no_llm and (missing := missing_credentials(config.model)):
        _fail(
            console,
            f"{config.model} needs [bold]{missing}[/] to be set.\n"
            "[dim]Set it, pick another model with --model, or run --no-llm.[/]",
        )

    provider: Provider | None = "gitlab" if mr else "github" if pr else None
    token: str | None = None
    target: Target | None = None

    if provider is not None:
        token = get_token(provider)
        if token is None:
            _fail(console, f"No {provider} token found. Set {TOKEN_HELP[provider]}.")
        try:
            target = parse_target((mr or pr or "").strip(), provider, host=detect_host(provider))
        except SourceError as exc:
            _fail(console, str(exc))

    try:
        changeset = _load_changeset(
            repo, provider, target, token, base, committed, uncommitted, include_untracked, console
        )
    except SourceError as exc:
        _fail(console, str(exc))

    static_findings: list[Finding] = []
    if config.static.enabled and changeset.origin == "local":
        # Static tools need the files on disk, which only the local source guarantees.
        with console.status("[dim]running static analysis…[/]", spinner="dots"):
            static_findings = StaticRunner(repo=repo, config=config.static).run(changeset)
    elif config.static.enabled:
        log.debug("skipping static analysis: %s changes are not checked out", changeset.origin)

    llm = None if no_llm else LLMClient(config.llm)
    status = f"reviewing with {config.model}…" if llm else "collecting findings…"
    with console.status(f"[dim]{status}[/]", spinner="dots"):
        result = Reviewer(
            config=config, repo=repo, llm=llm, static_findings=static_findings
        ).review(changeset)

    terminal.render(result, console, repo)

    if post and target is not None and token is not None:
        _publish(console, repo, target, token, result, no_summary=no_summary, repost=repost)

    if result.errors:
        raise typer.Exit(EXIT_ERROR)
    if fail_on and any(f.severity.at_least(fail_on) for f in result.findings):
        raise typer.Exit(EXIT_FINDINGS)
    raise typer.Exit(EXIT_OK)


def _load_changeset(
    repo: Path,
    provider: Provider | None,
    target: Target | None,
    token: str | None,
    base: str | None,
    committed: bool,
    uncommitted: bool,
    include_untracked: bool,
    console: Console,
) -> ChangeSet:
    if provider is not None:
        assert target is not None and token is not None  # guaranteed by the caller
        label = "merge request" if provider == "gitlab" else "pull request"
        source = GitLabSource if provider == "gitlab" else GitHubSource
        with console.status(f"[dim]fetching {label}…[/]", spinner="dots"):
            return source(target=target, token=token).load()

    scope = Scope.COMMITTED if committed else Scope.UNCOMMITTED if uncommitted else Scope.ALL
    return LocalGitSource(
        repo=repo, scope=scope, base=base, include_untracked=include_untracked
    ).load()


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


def _fail(console: Console, message: str) -> None:
    console.print(f"[bold red]error[/] {message}")
    raise typer.Exit(EXIT_ERROR)
