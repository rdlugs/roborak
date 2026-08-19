"""Render the Jinja prompt templates."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from roborak.context.diff import render_hunk_with_line_numbers
from roborak.core.config import Config
from roborak.core.models import ChangedFile, ChangeSet, Finding

PROMPT_DIR = Path(__file__).parent / "prompts"

_env = Environment(
    loader=FileSystemLoader(PROMPT_DIR),
    undefined=StrictUndefined,  # a missing variable should fail loudly, not silently
    trim_blocks=True,
    lstrip_blocks=True,
    keep_trailing_newline=True,
)


@dataclass
class RenderedPrompt:
    system: str
    user: str


def render_file_diff(file: ChangedFile) -> str:
    """The diff body for one file, annotated with new-file line numbers."""
    return "\n\n".join(render_hunk_with_line_numbers(hunk) for hunk in file.hunks)


def build_review_prompt(
    changeset: ChangeSet,
    config: Config,
    *,
    rules: list[object] | None = None,
    static_findings: list[Finding] | None = None,
    repo_context: str = "",
) -> RenderedPrompt:
    files = [
        {
            "path": f.path,
            "change_type": f.change_type,
            "language": f.language,
            "previous_path": f.previous_path,
            "rendered": render_file_diff(f),
        }
        for f in changeset.files
        if f.hunks
    ]

    language_notes = _language_notes(changeset, config)

    system = _env.get_template("review_system.jinja2").render(
        categories=[c.value for c in config.review.categories],
        max_findings=config.review.max_findings,
        committable_suggestions=config.review.committable_suggestions,
        full_file=config.review.full_file,
    )
    user = _env.get_template("review_user.jinja2").render(
        title=changeset.title,
        description=changeset.description,
        repo_context=repo_context,
        rules=rules or [],
        static_findings=static_findings or [],
        language_notes=language_notes,
        files=files,
        omitted_files=changeset.omitted_files,
    )
    return RenderedPrompt(system=system, user=user)


def _language_notes(changeset: ChangeSet, config: Config) -> str:
    """Pull in per-language guidance for the languages actually present."""
    present = {f.language for f in changeset.files if f.language}
    notes = [
        f"- {lang}: {config.language_instructions[lang]}"
        for lang in sorted(present)
        if lang in config.language_instructions
    ]
    return "\n".join(notes)
