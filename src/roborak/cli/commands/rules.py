"""``roborak rules`` — manage the team's own review standards."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from roborak.cli.shared import EXIT_OK, fail
from roborak.context.diff import detect_language
from roborak.core.config import load_config
from roborak.core.models import ChangedFile
from roborak.rules.loader import EXAMPLE_RULE, RuleError, load_rules, parse_rule
from roborak.rules.matcher import applies_to

rules_app = typer.Typer(help="Inspect and test the project's review rules.")


@rules_app.command("list")
def list_rules(
    repo: Annotated[Path | None, typer.Option("--dir", "-C")] = None,
) -> None:
    """Show every rule roborak will apply in this repository."""
    console = Console()
    repo = (repo or Path.cwd()).resolve()
    config = load_config(repo)
    rules = load_rules(repo, config.rules_dir)

    if not rules:
        console.print(
            f"[yellow]No rules found in [bold]{config.rules_dir}/[/].[/]\n"
            "[dim]Run `roborak rules init` to create one.[/]"
        )
        raise typer.Exit(EXIT_OK)

    table = Table(title=f"{len(rules)} rule(s) in {config.rules_dir}/")
    table.add_column("id", style="bold")
    table.add_column("severity")
    table.add_column("category")
    table.add_column("applies to", overflow="fold")

    for rule in rules:
        table.add_row(
            rule.id,
            rule.severity.value,
            rule.category.value,
            ", ".join(rule.paths) if rule.paths else "all files",
        )
    console.print(table)
    raise typer.Exit(EXIT_OK)


@rules_app.command("test")
def test_rule(
    rule_path: Annotated[Path, typer.Argument(help="The rule file to check.")],
    target: Annotated[
        Path | None, typer.Argument(help="A file to check the rule's scope against.")
    ] = None,
) -> None:
    """Validate one rule file, and optionally show whether it matches a given file."""
    console = Console()
    try:
        rule = parse_rule(rule_path)
    except RuleError as exc:
        fail(console, f"{rule_path}: {exc}")

    console.print(f"[green]✓[/] [bold]{rule.id}[/] parses cleanly")
    console.print(f"  severity: {rule.severity.value}   category: {rule.category.value}")
    console.print(f"  paths:    {', '.join(rule.paths) if rule.paths else 'all files'}")
    if rule.languages:
        console.print(f"  languages: {', '.join(rule.languages)}")
    console.print(f"\n[dim]{rule.body}[/]")

    if target is not None:
        path = str(target)
        candidate = ChangedFile(path=path, language=detect_language(path))
        if applies_to(rule, candidate):
            console.print(f"\n[green]✓[/] applies to [bold]{path}[/]")
        else:
            console.print(f"\n[yellow]✗[/] does not apply to [bold]{path}[/]")
    raise typer.Exit(EXIT_OK)


@rules_app.command("init")
def init_rules(
    repo: Annotated[Path | None, typer.Option("--dir", "-C")] = None,
) -> None:
    """Create the rules directory with a worked example."""
    console = Console()
    repo = (repo or Path.cwd()).resolve()
    config = load_config(repo)

    directory = repo / config.rules_dir
    example = directory / "no-raw-sql.md"
    if example.exists():
        fail(console, f"{example} already exists; not overwriting it.")

    try:
        directory.mkdir(parents=True, exist_ok=True)
        example.write_text(EXAMPLE_RULE, encoding="utf-8")
    except OSError as exc:
        fail(console, f"could not write {example}: {exc}")

    console.print(f"[green]created[/] {example}")
    console.print("[dim]Edit it, or add more .md files alongside it.[/]")
    raise typer.Exit(EXIT_OK)
