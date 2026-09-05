"""``roborak setup`` - a guided first run.

``config init`` writes the whole commented template with every option at its
default, which is the right reference file but leaves the two things that
actually block a first review - a model and a credential for it - for the user to
hand-edit in. This asks for those, and writes only what was answered, so every
other default stays live and follows roborak's releases.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Annotated, Any

import questionary
import typer
import yaml
from pydantic import ValidationError
from rich.console import Console

from roborak.cli.shared import EXIT_OK, TOKEN_HELP, fail
from roborak.cli.shared import is_interactive as _is_interactive
from roborak.core import config as core_config
from roborak.core.config import PROJECT_CONFIG_NAMES, Config, ForgeConfig, LLMConfig
from roborak.llm.client import missing_credentials, provider_of
from roborak.sources.forge import Provider, get_token


class Aborted(Exception):
    """The user pressed Ctrl-C or stdin ended. Nothing has been written."""


#: Returned by ``_select`` when the user wants to type an answer instead. A
#: control character so no real answer can collide with it.
OTHER = "\x00other"

#: A starting point for the model question, not a ceiling -- roborak takes any
#: LiteLLM model string, which is what ``Other (type it in)`` is for. The
#: default comes first so Enter alone still picks it.
KNOWN_MODELS = (
    "anthropic/claude-sonnet-5",
    "anthropic/claude-opus-5",
    "anthropic/claude-haiku-4-5-20251001",
    "openai/gpt-5",
    "gemini/gemini-2.5-pro",
)

HINT_CONFIG_INIT = "Run `rk config init` for a template to edit instead."

#: The lists have to look like the rest of roborak, not like whatever
#: ``questionary`` ships. Same reading as everywhere else in the CLI: cyan is a
#: path or a thing you are pointed at, green is a settled answer, dim is an aside.
#: Named ANSI colours rather than hex, so a user's terminal theme still wins.
SELECT_STYLE = questionary.Style(
    [
        ("qmark", "fg:ansicyan bold"),
        ("question", "bold"),
        ("pointer", "fg:ansicyan bold"),
        ("highlighted", "fg:ansicyan bold"),
        ("selected", "fg:ansicyan"),
        ("answer", "fg:ansigreen bold"),
        ("instruction", "fg:ansibrightblack"),
    ]
)


def _select(label: str, choices: list[questionary.Choice]) -> str:
    """Ask one closed-set question with the arrow keys.

    Every list ends with an escape hatch, because a curated list must never be a
    ceiling. ``questionary`` reports the two ways out differently -- Ctrl-C comes
    back as ``None``, a stdin that ends raises ``EOFError`` -- and both become the
    same ``Aborted`` the line-based prompts raise, so the caller has one thing to
    catch.
    """
    choices = [*choices, questionary.Choice(title="Other (type it in)…", value=OTHER)]
    try:
        answer = questionary.select(label, choices=choices, qmark="?", style=SELECT_STYLE).ask()
    except (EOFError, KeyboardInterrupt):
        raise Aborted from None
    if answer is None:
        raise Aborted
    return str(answer)


def _prompt(
    console: Console, label: str, *, default: str | None = None, secret: bool = False
) -> str:
    """Ask one question. An empty answer takes the default, or means 'skip'."""
    hint = f" [dim](default {default})[/]" if default else ""
    console.print(f"[bold]{label}[/]{hint}", highlight=False)
    try:
        answer = console.input("[bold cyan]>[/] ", password=secret and sys.stdin.isatty()).strip()
    except (EOFError, KeyboardInterrupt):
        console.print()
        raise Aborted from None
    if secret:
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
    except OSError:
        return False


def _ask_destination(console: Console, repo: Path) -> Path:
    """Where the file goes. Two known answers, so it is a list on a terminal."""
    project = repo / PROJECT_CONFIG_NAMES[0]
    console.print(
        "[dim]This file may end up holding API keys and forge tokens, so pick where it "
        "should live.[/]",
        highlight=False,
    )
    if not _is_interactive():
        return _ask_destination_by_number(console, project)

    answer = _select(
        "Where should the config go?",
        [
            questionary.Choice(title=f"{core_config.USER_CONFIG_PATH}  (user-wide)", value="user"),
            questionary.Choice(title=f"{project}  (this repository)", value="project"),
        ],
    )
    if answer == "user":
        return core_config.USER_CONFIG_PATH
    if answer == "project":
        return project
    typed = _prompt(console, "Where should the config go?", default=str(project))
    return Path(typed).expanduser()


def _ask_destination_by_number(console: Console, project: Path) -> Path:
    """The line-based fallback, for a pipe or a CI runner with no terminal."""
    console.print(
        f"  [bold]1[/] [cyan]{core_config.USER_CONFIG_PATH}[/]  [dim](user-wide)[/]",
        highlight=False,
    )
    console.print(f"  [bold]2[/] [cyan]{project}[/]  [dim](this repository)[/]", highlight=False)
    while True:
        answer = _prompt(console, "Where should the config go?", default="1")
        if answer == "1":
            return core_config.USER_CONFIG_PATH
        if answer == "2":
            return project
        console.print("[yellow]Answer 1 or 2.[/]")


def _ask_model(console: Console) -> str:
    """A model, picked from the known ones or typed in full."""
    default = LLMConfig().model
    if _is_interactive():
        answer = _select(
            "Which model?",
            [
                questionary.Choice(
                    title=f"{model}   (default)" if model == default else model, value=model
                )
                for model in KNOWN_MODELS
            ],
        )
        if answer != OTHER:
            return answer
    return _prompt(console, "Which model?", default=default)


def _ask_credential(console: Console, model: str, data: dict[str, Any]) -> None:
    """Fill in a key or an api_base, unless the environment already answers it."""
    provider = provider_of(model)
    required = missing_credentials(model, LLMConfig(model=model))
    if required is None:
        console.print(
            f"[dim]{model} already has a credential in the environment.[/]", highlight=False
        )
        return

    console.print(
        f"[dim]{model} needs [/][yellow]{required}[/][dim], which is not set. Leave this blank "
        f"if you use a proxy or a local model that needs no key.[/]",
        highlight=False,
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
        console.print(f"[dim]optional - {TOKEN_HELP[provider]}[/]", highlight=False)
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

    # No terminal is not fatal: the questions fall back to plain line prompts, so
    # a pipe can answer them. With nothing on stdin the first one hits EOF and
    # aborts, which is what keeps a CI runner from hanging here.
    try:
        destination = _ask_destination(console, repo)
        if destination.exists() and not force:
            fail(console, f"{destination} already exists. Pass --force to overwrite it.")

        data: dict[str, Any] = {"version": 1, "llm": {}}
        data["llm"]["model"] = _ask_model(console)
        _ask_credential(console, data["llm"]["model"], data)
        _ask_forge(console, data)
    except Aborted:
        console.print("[yellow]aborted[/] nothing was written.")
        if not _is_interactive():
            console.print(f"[dim]{HINT_CONFIG_INIT}[/]")
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
            destination.chmod(0o600)
    except OSError as exc:
        fail(console, f"could not write {destination}: {exc}")

    console.print(f"[green]created[/] [cyan]{destination}[/]", highlight=False)
    if holds_secrets and destination.parent == repo and not _is_gitignored(repo, destination):
        console.print(
            f"[yellow]warning[/] {destination.name} holds secrets and git does not "
            f"ignore it. Add it to .gitignore before committing.",
            highlight=False,
        )
    console.print("[dim]check it with: rk config show[/]")
    raise typer.Exit(EXIT_OK)
