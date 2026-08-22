"""Load a team's own review standards.

Rules are markdown with YAML frontmatter: a sentence of plain language beats a
regex for anything a linter cannot already catch, and it stays readable to the
people who have to agree with it.

    ---
    id: no-raw-sql
    paths: ["app/**/*.php"]
    severity: major
    category: security
    ---
    Never build SQL by string concatenation. Use the query builder or bound
    parameters.
"""

from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, ValidationError

from roborak.core.severity import Category, Severity

log = logging.getLogger(__name__)

_FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n(.*)\Z", re.DOTALL)


class Rule(BaseModel):
    id: str
    body: str
    severity: Severity = Severity.MAJOR
    category: Category = Category.MAINTAINABILITY
    paths: list[str] = Field(default_factory=list)
    """Glob patterns this rule applies to. Empty means every file."""

    languages: list[str] = Field(default_factory=list)
    enabled: bool = True
    source_path: str | None = None

    @property
    def qualified_id(self) -> str:
        return f"roborak/{self.id}"


class RuleError(ValueError):
    """A rule file could not be read. Reported with its path, never silently ignored."""


def load_rules(repo: Path, rules_dir: str) -> list[Rule]:
    """Load every rule under ``rules_dir``, skipping (and reporting) broken ones."""
    directory = repo / rules_dir
    if not directory.is_dir():
        return []

    rules: list[Rule] = []
    for path in sorted(directory.rglob("*.md")):
        try:
            rules.append(parse_rule(path, repo))
        except RuleError as exc:
            log.warning("skipping rule %s: %s", path, exc)
    return [rule for rule in rules if rule.enabled]


def load_rules_at_ref(repo: Path, rules_dir: str, ref: str) -> list[Rule] | None:
    """Load trusted rules from a git revision; ``None`` means the ref was unavailable."""
    try:
        listed = subprocess.run(
            ["git", "ls-tree", "-r", "--name-only", ref, "--", rules_dir],
            cwd=repo,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if listed.returncode != 0:
        return None

    rules: list[Rule] = []
    for name in sorted(line for line in listed.stdout.splitlines() if line.endswith(".md")):
        try:
            shown = subprocess.run(
                ["git", "show", f"{ref}:{name}"],
                cwd=repo,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if shown.returncode != 0:
            continue
        try:
            rules.append(parse_rule_text(shown.stdout, Path(name), repo))
        except RuleError as exc:
            log.warning("skipping rule %s at %s: %s", name, ref, exc)
    return [rule for rule in rules if rule.enabled]


def parse_rule(path: Path, repo: Path | None = None) -> Rule:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise RuleError(str(exc)) from exc

    return parse_rule_text(text, path, repo)


def parse_rule_text(text: str, path: Path, repo: Path | None = None) -> Rule:
    """Parse rule content supplied by either the filesystem or a git revision."""
    match = _FRONTMATTER.match(text)
    if match is None:
        body = text.strip()
        if not body:
            raise RuleError("the file is empty")
        return Rule(id=path.stem, body=body, source_path=_relative(path, repo))

    try:
        meta = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError as exc:
        raise RuleError(f"invalid frontmatter: {exc}") from exc
    if not isinstance(meta, dict):
        raise RuleError("frontmatter must be a mapping")

    body = match.group(2).strip()
    if not body:
        raise RuleError("the rule has frontmatter but no text")

    meta["id"] = _clean_id(meta.get("id"), path)
    meta["body"] = body
    meta["source_path"] = _relative(path, repo)

    try:
        return Rule.model_validate(meta)
    except ValidationError as exc:
        raise RuleError(_first_error(exc)) from exc


def _clean_id(raw: object, path: Path) -> str:
    """Fall back to the filename when the frontmatter id is unusable.

    PyYAML implements YAML 1.1, in which the bare words ``on``, ``off``, ``yes``
    and ``no`` parse as booleans -- so ``id: on`` arrives here as ``True``. That is
    always a misparse rather than intent, and the filename is the better answer.
    """
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    if raw is not None:
        log.warning(
            "rule %s has an unusable id (%r); using the filename instead. "
            "Quote it if you meant it literally.",
            path.name,
            raw,
        )
    return path.stem


def _relative(path: Path, repo: Path | None) -> str:
    if repo is None:
        return str(path)
    try:
        return str(path.relative_to(repo))
    except ValueError:
        return str(path)


def _first_error(exc: ValidationError) -> str:
    error = exc.errors()[0]
    field = ".".join(str(part) for part in error["loc"])
    return f"{field}: {error['msg']}"


EXAMPLE_RULE = """\
---
id: no-raw-sql
paths: ["**/*.py", "**/*.php"]
severity: major
category: security
---
Never build SQL by string concatenation or f-string interpolation. Use bound
parameters or the query builder, even when the value looks like it cannot come
from a user.
"""
