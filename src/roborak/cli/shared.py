"""Pieces every command needs.

Loading configuration, working out where the change comes from, and emitting the
result all behave identically across ``review``, ``describe``, ``improve`` and
``ask``, so they live here rather than being reimplemented four times.
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

import typer
from rich.console import Console
from rich.markdown import Markdown

from roborak.core.config import Config, ForgeConfig, load_config
from roborak.core.models import ChangeSet, Issue, ReviewResult, ReviewStatus
from roborak.core.severity import Severity
from roborak.llm.client import LLMClient, missing_credentials
from roborak.render import json_out, markdown, prompt_only, terminal
from roborak.sources.base import SourceError
from roborak.sources.forge import (
    Provider,
    Target,
    detect_provider,
    get_token,
    parse_target,
    provider_from_url,
    resolve_host,
)
from roborak.sources.github import GitHubSource
from roborak.sources.gitlab import GitLabSource
from roborak.sources.issue import load_issue, resolve_linked_change
from roborak.sources.local_git import LocalGitSource, Scope

log = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_FINDINGS = 1
EXIT_ERROR = 2

TOKEN_HELP = {
    "gitlab": "GITLAB_TOKEN (or ROBORAK_GITLAB_TOKEN), or put it in the config as "
    "forge.tokens.gitlab",
    "github": "GITHUB_TOKEN (or ROBORAK_GITHUB_TOKEN), put it in the config as "
    "forge.tokens.github, or sign in with `gh auth login`",
}


def stdout_is_a_terminal() -> bool:
    """Whether the report is being read or captured.

    The two want opposite things -- a person wants it rendered, a pipe wants the
    markdown -- and this is the only honest way to tell them apart.
    """
    return sys.stdout.isatty()


def fail(console: Console, message: str) -> NoReturn:
    console.print(f"[bold red]error[/] {message}")
    raise typer.Exit(EXIT_ERROR)


@dataclass
class Session:
    """Everything resolved from the flags, before any review work happens."""

    console: Console
    repo: Path
    config: Config
    changeset: ChangeSet
    llm: LLMClient | None
    target: Target | None
    token: str | None
    issue: Issue | None = None
    """What the change is supposed to solve, when ``--issue`` supplied one."""


def start(
    console: Console,
    *,
    repo: Path | None,
    mr: str | None,
    pr: str | None,
    issue: str | None = None,
    base: str | None = None,
    committed: bool = False,
    uncommitted: bool = False,
    include_untracked: bool = False,
    no_discussions: bool = False,
    config_path: Path | None = None,
    model: str | None = None,
    no_llm: bool = False,
    quiet_status: bool = False,
) -> Session:
    """Resolve config, credentials and the changeset, failing fast and clearly."""
    repo = (repo or Path.cwd()).resolve()

    if mr and pr:
        fail(console, "--mr and --pr are mutually exclusive.")
    if committed and uncommitted:
        fail(console, "--committed and --uncommitted are mutually exclusive.")

    try:
        config = load_config(repo, config_path)
    except (OSError, ValueError) as exc:
        fail(console, f"config error: {exc}")

    if model:
        config.llm.model = model

    # Credentials before work: a missing key should fail in a second, not after a
    # diff has been fetched over the network.
    if not no_llm and (missing := missing_credentials(config.model, config.llm)):
        fail(
            console,
            f"{config.model} needs [bold]{missing}[/] to be set.\n"
            "[dim]Set it, pick another model with --model, or run --no-llm.[/]",
        )

    provider: Provider | None = "gitlab" if mr else "github" if pr else None
    target: Target | None = None
    token: str | None = None

    loaded_issue: Issue | None = None
    if issue:
        stated_target = _has_target(
            mr=mr,
            pr=pr,
            base=base,
            committed=committed,
            uncommitted=uncommitted,
            include_untracked=include_untracked,
        )
        loaded_issue, linked = _load_issue(
            console,
            issue,
            repo=repo,
            forge=config.forge,
            mr=mr,
            pr=pr,
            quiet=quiet_status,
            resolve_link=not stated_target,
        )
        if linked is not None:
            provider, target = linked.provider, linked
            label = "merge request" if provider == "gitlab" else "pull request"
            console.print(f"[dim]issue #{loaded_issue.number} → {label} #{target.number}[/]")

    if provider is not None:
        token = get_token(provider, config.forge)
        if token is None:
            fail(console, f"No {provider} token found. Set {TOKEN_HELP[provider]}.")
        if target is None:
            try:
                target = parse_target(
                    (mr or pr or "").strip(),
                    provider,
                    host=resolve_host(provider, config.forge, repo=repo),
                    repo=repo,
                )
            except SourceError as exc:
                fail(console, str(exc))

    try:
        changeset = _load_changeset(
            console,
            repo,
            provider,
            target,
            token,
            max_recovered_file_bytes=config.forge.max_recovered_file_bytes,
            base=base,
            committed=committed,
            uncommitted=uncommitted,
            include_untracked=include_untracked,
            include_discussions=config.review.include_discussions and not no_discussions,
            quiet=quiet_status,
        )
    except SourceError as exc:
        fail(console, str(exc))

    return Session(
        console=console,
        repo=repo,
        config=config,
        changeset=changeset,
        llm=None if no_llm else LLMClient(config.llm),
        target=target,
        token=token,
        issue=loaded_issue,
    )


def _has_target(
    *,
    mr: str | None,
    pr: str | None,
    base: str | None,
    committed: bool,
    uncommitted: bool,
    include_untracked: bool,
) -> bool:
    """Whether the user already said what to review.

    ``--issue`` only picks the target when nothing else did; asking for an issue
    *and* a base ref means the issue is context for the diff you named.
    """
    return bool(mr or pr or base or committed or uncommitted or include_untracked)


def _issue_provider(
    console: Console,
    reference: str,
    *,
    mr: str | None,
    pr: str | None,
    repo: Path,
    forge: ForgeConfig,
) -> Provider:
    """Work out which forge holds the issue, or fail saying how to be explicit."""
    provider = (
        ("gitlab" if mr else "github" if pr else None)
        or provider_from_url(reference)
        or detect_provider(repo=repo, forge=forge)
    )
    if provider is None:
        fail(
            console,
            "Could not tell which forge issue "
            f"[bold]{reference}[/] lives on.\n"
            "[dim]Pass the full issue URL, add --mr/--pr to say which forge, or name "
            "the host in the config as forge.hosts.gitlab / forge.hosts.github.[/]",
        )
    return provider


def _load_issue(
    console: Console,
    reference: str,
    *,
    repo: Path,
    forge: ForgeConfig,
    mr: str | None,
    pr: str | None,
    quiet: bool,
    resolve_link: bool,
) -> tuple[Issue, Target | None]:
    """Fetch the issue, and the change implementing it when we are choosing one."""
    reference = reference.strip()
    provider = _issue_provider(console, reference, mr=mr, pr=pr, repo=repo, forge=forge)

    # Parse first: a malformed reference is the user's typo, and saying so beats
    # complaining about a token they would only need if the reference were valid.
    try:
        issue_target = parse_target(
            reference,
            provider,
            host=resolve_host(provider, forge, repo=repo),
            kind="issue",
            repo=repo,
        )
    except SourceError as exc:
        fail(console, str(exc))

    token = get_token(provider, forge)
    if token is None:
        fail(console, f"--issue needs a {provider} token. Set {TOKEN_HELP[provider]}.")

    try:
        if quiet:
            issue = load_issue(issue_target, token)
        else:
            with console.status("[dim]fetching issue…[/]", spinner="dots"):
                issue = load_issue(issue_target, token)

        if not resolve_link:
            return issue, None

        if quiet:
            linked = resolve_linked_change(issue_target, token)
        else:
            with console.status("[dim]looking for a linked change…[/]", spinner="dots"):
                linked = resolve_linked_change(issue_target, token)
    except SourceError as exc:
        fail(console, str(exc))

    if linked is None:
        log.debug("issue #%d has no linked change; reviewing the local diff", issue.number)
        return issue, None

    return issue, Target(provider, issue_target.host, issue_target.project, linked.number)


def _load_changeset(
    console: Console,
    repo: Path,
    provider: Provider | None,
    target: Target | None,
    token: str | None,
    *,
    max_recovered_file_bytes: int,
    base: str | None,
    committed: bool,
    uncommitted: bool,
    include_untracked: bool,
    include_discussions: bool,
    quiet: bool,
) -> ChangeSet:
    if provider is not None:
        assert target is not None and token is not None  # guaranteed by the caller
        label = "merge request" if provider == "gitlab" else "pull request"
        source = GitLabSource if provider == "gitlab" else GitHubSource
        instance = source(target=target, token=token)
        instance.max_recovered_file_bytes = max_recovered_file_bytes
        instance.include_discussions = include_discussions
        if quiet:
            return instance.load()
        with console.status(f"[dim]fetching {label}…[/]", spinner="dots"):
            return instance.load()

    scope = Scope.COMMITTED if committed else Scope.UNCOMMITTED if uncommitted else Scope.ALL
    return LocalGitSource(
        repo=repo, scope=scope, base=base, include_untracked=include_untracked
    ).load()


def emit(
    session: Session,
    result: ReviewResult,
    *,
    as_json: bool = False,
    agent: bool = False,
    prompt_only_mode: bool = False,
    markdown_path: Path | None = None,
    panels: bool = False,
) -> None:
    """Write the result to whichever surfaces were asked for.

    stdout carries the report and nothing else. Everything roborak says *about*
    the run -- spinners, errors, the closing question -- goes to stderr, so
    ``roborak review > review.md`` produces the report rather than the report with
    a spinner smeared through it. The machine-readable modes rely on the same
    property, which they always did.
    """
    console = session.console

    if markdown_path is not None:
        try:
            markdown_path.write_text(markdown.render(result), encoding="utf-8")
        except OSError as exc:
            console.print(f"[bold red]error[/] could not write {markdown_path}: {exc}")

    if agent or as_json:
        print(json_out.render(result, agent=agent))
        return
    if prompt_only_mode:
        print(prompt_only.render(result))
        return

    if panels:
        terminal.render(result, console, session.repo)
    elif stdout_is_a_terminal():
        # A person is reading: render it, with the collapsible sections opened
        # out into headings so nothing is lost to rich dropping the HTML.
        Console().print(Markdown(markdown.render(result, collapsible=False)))
    else:
        # Being captured. Plain print, not console.print: rich would read
        # `[...]` as markup and hard-wrap the lines, and `roborak review >
        # review.md` has to give back the document that gets published.
        print(markdown.render(result))

    if markdown_path is not None:
        console.print(f"[dim]report written to {markdown_path}[/]")


def finish(result: ReviewResult, fail_on: Severity | None) -> None:
    """Translate the result into an exit code."""
    if result.errors or result.status is not ReviewStatus.COMPLETE:
        raise typer.Exit(EXIT_ERROR)
    if fail_on is not None and any(f.severity.at_least(fail_on) for f in result.findings):
        raise typer.Exit(EXIT_FINDINGS)
    raise typer.Exit(EXIT_OK)
