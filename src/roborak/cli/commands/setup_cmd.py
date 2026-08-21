"""``roborak setup`` — a guided first run.

``config init`` writes the whole commented template with every option at its
default, which is the right reference file but leaves the two things that
actually block a first review — a model and a credential for it — for the user to
hand-edit in. This asks for those, and writes only what was answered, so every
other default stays live and follows roborak's releases.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Annotated, Any

import typer
import yaml
from pydantic import ValidationError
from rich.console import Console

from roborak.cli.shared import EXIT_OK, TOKEN_HELP, fail
from roborak.core import config as core_config
from roborak.core.config import PROJECT_CONFIG_NAMES, Config, ForgeConfig, LLMConfig
from roborak.llm.client import missing_credentials, provider_of
from roborak.sources.forge import Provider, get_token


class Aborted(Exception):
    """The user pressed Ctrl-C or stdin ended. Nothing has been written."""


def _is_interactive() -> bool:
    """Whether there is a human here to answer.

    The same test ``review`` applies before it offers to publish: a redirected
    stdout means nobody is reading, and a non-tty stdin means nobody could answer.
    """
    return sys.stdout.isatty() and sys.stdin.isatty()


def _prompt(
    console: Console, label: str, *, default: str | None = None, secret: bool = False
) -> str:
    """Ask one question. An empty answer takes the default, or means 'skip'."""
    hint = f" [dim](default {default})[/]" if default else ""
    console.print(f"[bold]{label}[/]{hint}", highlight=False)
    try:
        # getpass opens /dev/tty, which has nothing to read from when stdin is a
        # pipe -- and echoing is moot there anyway, since nobody is watching.
        answer = console.input("> ", password=secret and sys.stdin.isatty()).strip()
    except (EOFError, KeyboardInterrupt):
        console.print()
        raise Aborted from None
    if secret:
        # password=True swallows the newline the user typed.
        console.print()
    return answer or (default or "")


def _is_gitignored(repo: Path, path: Path) -> bool:
    """Whether git would ignore this path. False whenever we cannot tell."""
    try:
        return (
            subprocess.run(
                ["git", "check-ignore", "-q", str(path)],
                cwd=repo,
                capture_output=True,
                check=False,
            ).returncode
            == 0
        )
    except OSError:  # no git on PATH, or repo is not a directory
        return False


def _ask_destination(console: Console, repo: Path) -> Path:
    project = repo / PROJECT_CONFIG_NAMES[0]
    console.print(
        "This file may end up holding API keys and forge tokens, so pick where it should live.",
        highlight=False,
    )
    console.print(f"  [bold]1[/] {core_config.USER_CONFIG_PATH}  [dim](user-wide)[/]")
    console.print(f"  [bold]2[/] {project}  [dim](this repository)[/]")
    while True:
        answer = _prompt(console, "Where should the config go?", default="1")
        if answer == "1":
            return core_config.USER_CONFIG_PATH
        if answer == "2":
            return project
        console.print("[yellow]Answer 1 or 2.[/]")


def _ask_credential(console: Console, model: str, data: dict[str, Any]) -> None:
    """Fill in a key or an api_base, unless the environment already answers it."""
    provider = provider_of(model)
    required = missing_credentials(model, LLMConfig(model=model))
    if required is None:
        console.print(f"[dim]{model} already has a credential in the environment.[/]")
        return

    console.print(
        f"[dim]{model} needs {required}, which is not set. Leave this blank if you "
        f"use a proxy or a local model that needs no key.[/]"
    )
    key = _prompt(console, f"API key for {provider}?", secret=True)
    if key:
        data["llm"]["api_keys"] = {provider: key}
        return

    api_base = _prompt(console, "An api_base instead? (blank to skip both)")
    if api_base:
        data["llm"]["api_base"] = api_base
    else:
        console.print("[yellow]note[/] no credential set; reviews will fail until one is.")


def _ask_forge(console: Console, data: dict[str, Any]) -> None:
    """Optional GitLab and GitHub tokens, and a host for whichever got one."""
    forge: dict[str, dict[str, str]] = {}
    for provider in ("gitlab", "github"):
        name: Provider = provider  # type: ignore[assignment]
        if get_token(name, ForgeConfig()):
            console.print(f"[dim]a {provider} token is already available; skipping.[/]")
            continue
        console.print(f"[dim]optional — {TOKEN_HELP[provider]}[/]", highlight=False)
        token = _prompt(console, f"{provider} token? (blank to skip)", secret=True)
        if not token:
            continue
        forge.setdefault("tokens", {})[provider] = token

        host = _prompt(console, f"Self-hosted {provider} domain? (blank for the default)")
        while host:
            candidate = dict(forge.get("hosts", {}))
            candidate[provider] = host
            try:
                validated = ForgeConfig(hosts=candidate)
            except ValidationError as exc:
                console.print(f"[yellow]{_first_message(exc)}[/]")
                host = _prompt(console, f"Self-hosted {provider} domain? (blank for the default)")
                continue
            # Keep what the validator made of it, not what was typed: the file
            # should hold the bare netloc `Target.host` expects everywhere.
            forge["hosts"] = dict(validated.hosts)
            break
    if forge:
        data["forge"] = forge


def _first_message(exc: ValidationError) -> str:
    errors = exc.errors()
    return errors[0]["msg"] if errors else str(exc)


def setup(
    repo: Annotated[Path | None, typer.Option("--dir", "-C")] = None,
    force: Annotated[bool, typer.Option("--force", help="Overwrite an existing file.")] = False,
) -> None:
    """Answer a few questions and write the config they imply."""
    console = Console()
    repo = (repo or Path.cwd()).resolve()

    if not _is_interactive():
        fail(
            console,
            "setup needs a terminal to ask questions. Run `rk config init` for a "
            "template to edit instead.",
        )

    try:
        destination = _ask_destination(console, repo)
        if destination.exists() and not force:
            fail(console, f"{destination} already exists. Pass --force to overwrite it.")

        data: dict[str, Any] = {"version": 1, "llm": {}}
        data["llm"]["model"] = _prompt(console, "Which model?", default=LLMConfig().model)
        _ask_credential(console, data["llm"]["model"], data)
        _ask_forge(console, data)
    except Aborted:
        console.print("[yellow]aborted[/] nothing was written.")
        raise typer.Exit(EXIT_OK) from None

    try:
        Config.model_validate(data)
    except ValidationError as exc:
        fail(console, f"that config is not valid: {_first_message(exc)}")

    holds_secrets = bool(data["llm"].get("api_keys")) or bool(data.get("forge", {}).get("tokens"))
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
        if holds_secrets:
            # The mode follows the content, not the path: unlike `config init`,
            # this wizard can put real secrets in a repository file.
            destination.chmod(0o600)
    except OSError as exc:
        fail(console, f"could not write {destination}: {exc}")

    console.print(f"[green]created[/] {destination}")
    if holds_secrets and destination.parent == repo and not _is_gitignored(repo, destination):
        console.print(
            f"[yellow]warning[/] {destination.name} holds secrets and git does not "
            f"ignore it. Add it to .gitignore before committing.",
            highlight=False,
        )
    console.print("[dim]check it with: rk config show[/]")
    raise typer.Exit(EXIT_OK)
