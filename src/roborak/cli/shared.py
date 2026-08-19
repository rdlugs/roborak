"""Pieces every command needs.

Loading configuration, working out where the change comes from, and emitting the
result all behave identically across ``review``, ``describe``, ``improve`` and
``ask``, so they live here rather than being reimplemented four times.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import typer
from rich.console import Console

from roborak.core.config import Config, load_config
from roborak.core.models import ChangeSet, ReviewResult
from roborak.core.severity import Severity
from roborak.llm.client import LLMClient, missing_credentials
from roborak.render import json_out, markdown, prompt_only, terminal
from roborak.sources.base import SourceError
from roborak.sources.forge import Provider, Target, detect_host, get_token, parse_target
from roborak.sources.github import GitHubSource
from roborak.sources.gitlab import GitLabSource
from roborak.sources.local_git import LocalGitSource, Scope

log = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_FINDINGS = 1
EXIT_ERROR = 2

TOKEN_HELP = {
    "gitlab": "GITLAB_TOKEN (or ROBORAK_GITLAB_TOKEN)",
    "github": "GITHUB_TOKEN, or sign in with `gh auth login`",
}


def fail(console: Console, message: str) -> None:
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


def start(
    console: Console,
    *,
    repo: Path | None,
    mr: str | None,
    pr: str | None,
    base: str | None = None,
    committed: bool = False,
    uncommitted: bool = False,
    include_untracked: bool = False,
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
    if not no_llm and (missing := missing_credentials(config.model)):
        fail(
            console,
            f"{config.model} needs [bold]{missing}[/] to be set.\n"
            "[dim]Set it, pick another model with --model, or run --no-llm.[/]",
        )

    provider: Provider | None = "gitlab" if mr else "github" if pr else None
    target: Target | None = None
    token: str | None = None

    if provider is not None:
        token = get_token(provider)
        if token is None:
            fail(console, f"No {provider} token found. Set {TOKEN_HELP[provider]}.")
        try:
            target = parse_target((mr or pr or "").strip(), provider, host=detect_host(provider))
        except SourceError as exc:
            fail(console, str(exc))

    try:
        changeset = _load_changeset(
            console,
            repo,
            provider,
            target,
            token,
            base=base,
            committed=committed,
            uncommitted=uncommitted,
            include_untracked=include_untracked,
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
    )


def _load_changeset(
    console: Console,
    repo: Path,
    provider: Provider | None,
    target: Target | None,
    token: str | None,
    *,
    base: str | None,
    committed: bool,
    uncommitted: bool,
    include_untracked: bool,
    quiet: bool,
) -> ChangeSet:
    if provider is not None:
        assert target is not None and token is not None  # guaranteed by the caller
        label = "merge request" if provider == "gitlab" else "pull request"
        source = GitLabSource if provider == "gitlab" else GitHubSource
        if quiet:
            return source(target=target, token=token).load()
        with console.status(f"[dim]fetching {label}…[/]", spinner="dots"):
            return source(target=target, token=token).load()

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
) -> None:
    """Write the result to whichever surfaces were asked for.

    Machine-readable modes go to stdout *alone*: anything else on the stream would
    break the consumer parsing it.
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

    terminal.render(result, console, session.repo)
    if markdown_path is not None:
        console.print(f"[dim]report written to {markdown_path}[/]")


def finish(result: ReviewResult, fail_on: Severity | None) -> None:
    """Translate the result into an exit code."""
    if result.errors:
        raise typer.Exit(EXIT_ERROR)
    if fail_on is not None and any(f.severity.at_least(fail_on) for f in result.findings):
        raise typer.Exit(EXIT_FINDINGS)
    raise typer.Exit(EXIT_OK)
