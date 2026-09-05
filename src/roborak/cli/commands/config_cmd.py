"""``roborak config`` - inspect and scaffold configuration."""

from __future__ import annotations

from importlib import resources
from pathlib import Path
from typing import Annotated

import typer
import yaml
from rich.console import Console
from rich.syntax import Syntax

from roborak.cli.shared import EXIT_OK, fail
from roborak.core import config as core_config
from roborak.core.config import PROJECT_CONFIG_NAMES, Config, load_config
from roborak.llm.client import missing_credentials

config_app = typer.Typer(help="Inspect and scaffold roborak's configuration.")

TEMPLATE_NAME = "config_template.yaml"
"""Ships inside the package, so an installed wheel scaffolds the same commented
file a source checkout does."""


def template_text() -> str:
    """The commented template, falling back to a bare dump if it went missing."""
    try:
        return (resources.files("roborak") / TEMPLATE_NAME).read_text(encoding="utf-8")
    except (FileNotFoundError, OSError, ModuleNotFoundError):
        return yaml.safe_dump(Config().model_dump(mode="json"), sort_keys=False)


@config_app.command("show")
def show_config(
    repo: Annotated[Path | None, typer.Option("--dir", "-C")] = None,
    config_path: Annotated[Path | None, typer.Option("--config")] = None,
) -> None:
    """Print the effective configuration, after every layer has been merged."""
    console = Console()
    repo = (repo or Path.cwd()).resolve()

    try:
        config = load_config(repo, config_path)
    except (OSError, ValueError) as exc:
        fail(console, f"config error: {exc}")

    console.print(
        Syntax(
            yaml.safe_dump(config.model_dump(mode="json"), sort_keys=False, width=88),
            "yaml",
            theme="ansi_dark",
        )
    )

    user_path = core_config.USER_CONFIG_PATH
    sources = [str(user_path)] if user_path.is_file() else []
    project_path = core_config.project_config_path(repo, config_path)
    if project_path is not None:
        sources.append(str(project_path))
    console.print(f"[dim]loaded from: {', '.join(sources) or 'defaults only'}[/]")

    if missing := missing_credentials(config.model, config.llm):
        console.print(f"[yellow]note[/] {config.model} needs {missing}, which is not set.")
    raise typer.Exit(EXIT_OK)


@config_app.command("init")
def init_config(
    repo: Annotated[Path | None, typer.Option("--dir", "-C")] = None,
    user: Annotated[
        bool,
        typer.Option(
            "--global",
            "-g",
            help="Write the user-wide config instead of this repository's.",
        ),
    ] = False,
    force: Annotated[bool, typer.Option("--force", help="Overwrite an existing file.")] = False,
) -> None:
    """Write a commented .roborak.yaml with every option at its default.

    .roborak.yaml is the canonical generated name; .roborak.yml is also accepted
    when loading project configuration. If both exist, .roborak.yaml wins.
    """
    console = Console()

    if user and repo is not None:
        fail(console, "--global writes one fixed path; -C has nothing to point at.")

    destination = (
        core_config.USER_CONFIG_PATH
        if user
        else (repo or Path.cwd()).resolve() / PROJECT_CONFIG_NAMES[0]
    )

    if destination.exists() and not force:
        fail(console, f"{destination} already exists. Pass --force to overwrite it.")

    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(template_text(), encoding="utf-8")
        if user:
            destination.chmod(0o600)
    except OSError as exc:
        fail(console, f"could not write {destination}: {exc}")

    console.print(f"[green]created[/] {destination}")
    if not user:
        console.print("[dim]for a user-wide config instead: rk config init --global[/]")
    raise typer.Exit(EXIT_OK)
