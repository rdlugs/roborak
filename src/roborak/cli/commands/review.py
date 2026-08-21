"""``roborak review`` — the main command."""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from roborak.analysis.reviewer import Reviewer
from roborak.cli import shared
from roborak.cli.shared import fail
from roborak.core.buckets import Bucket, group
from roborak.core.models import Finding, ReviewResult, ReviewStatus
from roborak.core.severity import Severity
from roborak.publish.base import PublishReport, remote_fingerprints
from roborak.publish.github import GitHubPublisher
from roborak.publish.gitlab import GitLabPublisher
from roborak.render import markdown
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
    issue: Annotated[
        str | None,
        typer.Option("--issue", help="Issue this change should solve: a number or a URL."),
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
    no_discussions: Annotated[
        bool,
        typer.Option("--no-discussions", help="Do not use existing MR/PR comments as context."),
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
    no_post: Annotated[
        bool,
        typer.Option("--no-post", help="Never offer to publish at the end of a review."),
    ] = False,
    no_walkthrough: Annotated[
        bool,
        typer.Option("--no-walkthrough", help="Skip the overview; one model call instead of two."),
    ] = False,
    panels: Annotated[
        bool,
        typer.Option("--panels", help="Show rich panels with code context instead of the report."),
    ] = False,
    full: Annotated[
        bool,
        typer.Option("--full", help="Add the agent prompts and review info the terminal hides."),
    ] = False,
    no_llm: Annotated[
        bool,
        typer.Option("--no-llm", help="Static analysis only; makes no model calls."),
    ] = False,
    no_static: Annotated[
        bool, typer.Option("--no-static", help="Skip static analysis; model only.")
    ] = False,
    trust_static: Annotated[
        bool,
        typer.Option(
            "--trust-static",
            help="Allow repository-provided static tools to execute directly in CI.",
        ),
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
    # Everything roborak says about the run goes to stderr; stdout is the report.
    console = Console(stderr=True, quiet=as_json or agent or prompt_only)

    if post and not (mr or pr or issue):
        fail(console, "--post needs --mr or --pr; there is nowhere to post a local review.")

    session = shared.start(
        console,
        repo=repo,
        mr=mr,
        pr=pr,
        issue=issue,
        base=base,
        committed=committed,
        uncommitted=uncommitted,
        include_untracked=include_untracked,
        no_discussions=no_discussions,
        config_path=config_path,
        model=model,
        no_llm=no_llm,
        quiet_status=as_json or agent or prompt_only,
    )

    if post and session.target is None:
        fail(
            console,
            f"--post needs a merge or pull request; issue {issue} has no linked change.",
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
    if trust_static:
        from roborak.core.config import StaticExecution

        config.static.execution = StaticExecution.TRUSTED
    if no_walkthrough:
        config.output.walkthrough = False
    if no_post:
        config.output.confirm_post = False
    if panels:
        config.output.panels = True
    if full:
        config.output.full = True

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

    reviewer = Reviewer(
        config=config,
        repo=session.repo,
        llm=session.llm,
        static_findings=static_findings,
        issue=session.issue,
    )

    status = f"reviewing with {config.model}…" if session.llm else "collecting findings…"
    with console.status(f"[dim]{status}[/]", spinner="dots"):
        result = reviewer.review(session.changeset)

    # A second call, so it gets its own spinner rather than sitting silently
    # behind a message that says the review is still running.
    if config.output.walkthrough and session.llm is not None and not session.changeset.is_empty:
        with console.status("[dim]writing the overview…[/]", spinner="dots"):
            result.walkthrough = reviewer.walkthrough(session.changeset)
        reviewer.apply_usage(result)

    if post and session.target is not None and session.token is not None:
        _publish(
            console,
            session.repo,
            session.target,
            session.token,
            result,
            post_inline=True,
            post_summary=not no_summary,
            repost=repost,
        )

    shared.emit(
        session,
        result,
        as_json=as_json,
        agent=agent,
        prompt_only_mode=prompt_only,
        markdown_path=markdown_out,
        panels=config.output.panels,
        full=config.output.full,
    )

    if not post:
        _offer_to_share(
            console,
            session,
            result,
            machine_mode=as_json or agent or prompt_only,
            no_summary=no_summary,
            repost=repost,
            already_written=markdown_out is not None,
        )

    shared.finish(result, fail_on)


def _seen_fingerprints(repo: Path, target: Target, *, repost: bool) -> frozenset[str]:
    """What an earlier run already posted, so we neither repeat nor re-offer it."""
    if repost:
        return frozenset()
    store = StateStore(repo)
    key = review_key(target.provider, target.host, target.project, target.number)
    return frozenset(store.get(key).fingerprints)


def _publish(
    console: Console,
    repo: Path,
    target: Target,
    token: str,
    result: ReviewResult,
    *,
    post_inline: bool,
    post_summary: bool,
    repost: bool,
) -> None:
    store = StateStore(repo)
    key = review_key(target.provider, target.host, target.project, target.number)
    seen = _seen_fingerprints(repo, target, repost=repost)
    if not repost:
        try:
            seen = frozenset({*seen, *remote_fingerprints(target, token)})
        except SourceError as exc:
            result.status = ReviewStatus.PARTIAL
            result.errors.append(f"could not discover existing review comments: {exc}")
            console.print(f"[bold red]could not post review[/] {exc}")
            return

    publisher_cls = GitLabPublisher if target.provider == "gitlab" else GitHubPublisher
    publisher = publisher_cls(
        target=target,
        token=token,
        post_inline=post_inline,
        post_summary=post_summary,
        seen_fingerprints=seen,
    )

    try:
        with console.status("[dim]posting review…[/]", spinner="dots"):
            report = publisher.publish(result)
    except SourceError as exc:
        console.print(f"[bold red]could not post review[/] {exc}")
        result.status = ReviewStatus.PARTIAL
        result.errors.append(f"could not post review: {exc}")
        return

    store.record(key, report.posted, result.changeset.head_sha if result.changeset else "")
    _report_publish(console, report)
    if report.failed:
        result.status = ReviewStatus.PARTIAL
        result.errors.append(f"could not post {len(report.failed)} inline comment(s)")


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


DEFAULT_REPORT_NAME = "roborak-review.md"


def _is_interactive() -> bool:
    """Whether there is a human here to answer.

    Both halves matter: a redirected stdout means the report is being captured
    rather than read, and a non-tty stdin means nobody could answer anyway.
    Typer's ``CliRunner`` and every CI runner fail the second test, which is what
    keeps them from hanging. Asked of the streams directly rather than of the
    console, because the console is stderr now and stderr staying a terminal says
    nothing about whether stdout was piped into a file.
    """
    return sys.stdout.isatty() and sys.stdin.isatty()


def _offer_to_share(
    console: Console,
    session: shared.Session,
    result: ReviewResult,
    *,
    machine_mode: bool,
    no_summary: bool,
    repost: bool,
    already_written: bool,
) -> None:
    """Ask, once the report is on screen, what to do with it.

    Deciding after reading the review is the point: ``--post`` has to be chosen
    before the model has said anything, and changing your mind afterwards costs a
    whole second run.
    """
    if machine_mode or not session.config.output.confirm_post:
        return
    if not _is_interactive():
        return
    if not result.findings and result.walkthrough is None:
        return

    changeset = result.changeset
    can_post = (
        session.target is not None
        and session.token is not None
        and changeset is not None
        and changeset.forge_ref is not None
    )
    # --markdown already wrote the file; offering to write it again is noise.
    can_save = not already_written
    if not (can_post or can_save):
        return

    console.print()
    if can_post:
        assert session.target is not None
        target = session.target
        console.print(
            f"[bold]Post to {target.host} {target.project} {_reference(target)}?[/]",
            highlight=False,
        )
        _print_preview(console, session, result, no_summary=no_summary, repost=repost)
    else:
        # A local diff has nowhere to post, so saving is the only thing on offer.
        console.print("[bold]Save this review?[/]", highlight=False)

    offered = [key for key, ok in (("[p] post", can_post), ("[s] save as .md", can_save)) if ok]
    # markup=False: rich would read `[p]` as a style tag and swallow it.
    # highlight=False: and its repr highlighter would then bold the brackets.
    console.print(f"\n  {'  '.join(offered)}  [n] no", markup=False, highlight=False)
    answer = _ask(console)

    if answer == "p" and can_post:
        assert session.target is not None and session.token is not None
        _publish(
            console,
            session.repo,
            session.target,
            session.token,
            result,
            post_inline=True,
            post_summary=not no_summary,
            repost=repost,
        )
    elif answer == "s" and can_save:
        _save(console, result, suggested=DEFAULT_REPORT_NAME)


def _print_preview(
    console: Console,
    session: shared.Session,
    result: ReviewResult,
    *,
    no_summary: bool,
    repost: bool,
) -> None:
    """What would go where, before a single request is made."""
    assert session.target is not None
    actionable = group(result).get(Bucket.ACTIONABLE, [])
    seen = _seen_fingerprints(session.repo, session.target, repost=repost)
    postable = [f for f in actionable if f.fingerprint not in seen]
    already = len(actionable) - len(postable)

    if not no_summary:
        console.print("  [dim]the report above, as a comment[/]", highlight=False)
    if postable:
        suffix = f" ({already} already posted earlier)" if already else ""
        console.print(f"  [dim]{len(postable)} inline comment(s){suffix}[/]", highlight=False)
    elif already:
        console.print(
            f"  [dim]nothing new inline; {already} already posted earlier[/]", highlight=False
        )


def _ask(console: Console) -> str | None:
    """Read one key. Anything that is not an offered choice means no."""
    try:
        return console.input("> ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        # A closed stdin or a Ctrl-C is a decision not to publish, not an error.
        console.print()
        return None


def _save(console: Console, result: ReviewResult, *, suggested: str | None) -> None:
    """Write the report, at a path the user names."""
    hint = f" [dim](default {suggested})[/]" if suggested else ""
    console.print(f"[bold]Path?[/]{hint}", highlight=False)
    try:
        answer = console.input("> ").strip()
    except (EOFError, KeyboardInterrupt):
        console.print()
        return

    if not answer:
        if suggested is None:
            return
        answer = suggested

    path = Path(answer).expanduser()
    try:
        path.write_text(markdown.render(result), encoding="utf-8")
    except OSError as exc:
        console.print(f"[bold red]error[/] could not write {path}: {exc}")
        return
    console.print(f"[dim]report written to {path}[/]")


def _reference(target: Target) -> str:
    """How each forge writes a change: GitLab takes ``!N``, GitHub ``#N``."""
    return f"!{target.number}" if target.provider == "gitlab" else f"#{target.number}"
