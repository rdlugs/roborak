"""``roborak config`` — inspect and scaffold configuration."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
import yaml
from rich.console import Console
from rich.syntax import Syntax

from roborak.cli.shared import EXIT_OK, fail
from roborak.core.config import PROJECT_CONFIG_NAMES, USER_CONFIG_PATH, Config, load_config
from roborak.llm.client import missing_credentials

config_app = typer.Typer(help="Inspect and scaffold roborak's configuration.")

TEMPLATE = Path(__file__).resolve().parents[4] / ".roborak.yaml.example"


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

    sources = [str(USER_CONFIG_PATH)] if USER_CONFIG_PATH.is_file() else []
    sources += [str(repo / name) for name in PROJECT_CONFIG_NAMES if (repo / name).is_file()]
    console.print(f"[dim]loaded from: {', '.join(sources) or 'defaults only'}[/]")

    if missing := missing_credentials(config.model, config.llm):
        console.print(f"[yellow]note[/] {config.model} needs {missing}, which is not set.")
    raise typer.Exit(EXIT_OK)


@config_app.command("init")
def init_config(
    repo: Annotated[Path | None, typer.Option("--dir", "-C")] = None,
    force: Annotated[bool, typer.Option("--force", help="Overwrite an existing file.")] = False,
) -> None:
    """Write a commented .roborak.yaml with every option at its default."""
    console = Console()
    repo = (repo or Path.cwd()).resolve()
    destination = repo / PROJECT_CONFIG_NAMES[0]

    if destination.exists() and not force:
        fail(console, f"{destination} already exists. Pass --force to overwrite it.")

    content = (
        TEMPLATE.read_text(encoding="utf-8")
        if TEMPLATE.is_file()
        else yaml.safe_dump(Config().model_dump(mode="json"), sort_keys=False)
    )
    try:
        destination.write_text(content, encoding="utf-8")
    except OSError as exc:
        fail(console, f"could not write {destination}: {exc}")

    console.print(f"[green]created[/] {destination}")
    raise typer.Exit(EXIT_OK)
