"""The pipeline that turns a changeset into a review.

Deliberately linear: source -> filter -> compress -> static -> LLM -> validate.
Each step is independently testable and none reaches backwards.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from roborak.analysis import validator
from roborak.context.compressor import MAX_HUNK_LINES, compress, filter_files
from roborak.context.diff import render_hunk_with_line_numbers
from roborak.core.config import Config
from roborak.core.models import ChangedFile, ChangeSet, Finding, ReviewResult
from roborak.llm.client import LLMClient, LLMError
from roborak.llm.parser import ParseError, parse_findings, parse_walkthrough
from roborak.llm.prompt import (
    build_ask_prompt,
    build_describe_prompt,
    build_improve_prompt,
    build_review_prompt,
)
from roborak.rules.loader import load_rules
from roborak.rules.matcher import matching_rules, rules_for_prompt

log = logging.getLogger(__name__)


def render_for_prompt(file: ChangedFile) -> str:
    return "\n\n".join(
        render_hunk_with_line_numbers(h, max_lines=MAX_HUNK_LINES) for h in file.hunks
    )


@dataclass
class Reviewer:
    config: Config
    repo: Path
    llm: LLMClient | None = None
    """``None`` runs the static-analysis-only path (``--no-llm``)."""

    static_findings: list[Finding] = field(default_factory=list)
    _rules: list[object] | None = field(default=None, repr=False)

    def rules_for(self, changeset: ChangeSet) -> list[dict[str, str]]:
        """The team's own rules that apply to this change, ready for the prompt."""
        if self._rules is None:
            self._rules = list(load_rules(self.repo, self.config.rules_dir))
        matched = matching_rules(self._rules, changeset)  # type: ignore[arg-type]
        if matched:
            log.debug("%d of %d rules apply to this change", len(matched), len(self._rules))
        return rules_for_prompt(matched)

    def review(self, changeset: ChangeSet) -> ReviewResult:
        result = ReviewResult(changeset=changeset, model=self.config.model if self.llm else None)

        if not self._prepare(changeset, result):
            return result

        findings = list(self.static_findings)
        if self.llm is not None:
            try:
                findings.extend(self._llm_findings(changeset))
            except (LLMError, ParseError) as exc:
                log.error("LLM review failed: %s", exc)
                result.errors.append(str(exc))

        result.findings = validator.validate(findings, changeset, self.config)
        return result

    def describe(self, changeset: ChangeSet) -> ReviewResult:
        """Produce a walkthrough instead of findings."""
        result = ReviewResult(changeset=changeset, model=self.config.model)
        if not self._prepare(changeset, result) or self.llm is None:
            return result

        prompt = build_describe_prompt(
            changeset, self.config, repo_context=load_repo_context(self.repo)
        )
        try:
            response = self.llm.complete(prompt.system, prompt.user)
            result.walkthrough = parse_walkthrough(response.text)
        except (LLMError, ParseError) as exc:
            log.error("describe failed: %s", exc)
            result.errors.append(str(exc))
        return result

    def improve(self, changeset: ChangeSet) -> ReviewResult:
        """Suggestion-only mode: every finding carries committable code."""
        result = ReviewResult(changeset=changeset, model=self.config.model)
        if not self._prepare(changeset, result) or self.llm is None:
            return result

        prompt = build_improve_prompt(
            changeset,
            self.config,
            rules=self.rules_for(changeset),  # type: ignore[arg-type]
            repo_context=load_repo_context(self.repo),
        )
        try:
            response = self.llm.complete(prompt.system, prompt.user)
            findings = parse_findings(response.text, valid_files={f.path for f in changeset.files})
        except (LLMError, ParseError) as exc:
            log.error("improve failed: %s", exc)
            result.errors.append(str(exc))
            return result

        # A suggestion with no code to commit is not a suggestion.
        findings = [f for f in findings if f.suggestion]
        result.findings = validator.validate(findings, changeset, self.config)
        return result

    def ask(self, changeset: ChangeSet, question: str) -> str:
        """Answer a free-text question about the change."""
        if self.llm is None:
            raise LLMError("`ask` needs a model; it cannot run with --no-llm.")

        filter_files(changeset, self.config.ignore_paths)
        if changeset.is_empty:
            return "There are no changes to ask about."

        compress(
            changeset, self.llm.context_budget, self.llm.count_tokens, render=render_for_prompt
        )
        prompt = build_ask_prompt(changeset, question, repo_context=load_repo_context(self.repo))
        return self.llm.complete(prompt.system, prompt.user).text.strip()

    def _prepare(self, changeset: ChangeSet, result: ReviewResult) -> bool:
        """Filter and compress in place. Returns False when nothing is left."""
        filter_files(changeset, self.config.ignore_paths)
        if changeset.is_empty:
            return False
        if self.llm is not None:
            compress(
                changeset,
                self.llm.context_budget,
                self.llm.count_tokens,
                render=render_for_prompt,
            )
        result.skipped_files = list(changeset.omitted_files)
        return True

    def _llm_findings(self, changeset: ChangeSet) -> list[Finding]:
        assert self.llm is not None
        prompt = build_review_prompt(
            changeset,
            self.config,
            rules=self.rules_for(changeset),  # type: ignore[arg-type]
            static_findings=self._static_for_prompt(),
            repo_context=load_repo_context(self.repo),
        )
        response = self.llm.complete(prompt.system, prompt.user)
        log.debug("model returned %d chars", len(response.text))
        return parse_findings(
            response.text,
            valid_files={f.path for f in changeset.files},
        )

    def _static_for_prompt(self) -> list[Finding]:
        if not (self.config.static.enabled and self.config.static.feed_to_llm):
            return []
        return self.static_findings[: self.config.static.max_findings_in_prompt]


# Files a repo uses to tell tools how it wants to be treated. Read in order; the
# first that exists wins, so a repo can point roborak at AGENTS.md without also
# feeding it a CLAUDE.md meant for a different agent.
CONTEXT_FILES = ("AGENTS.md", "CLAUDE.md", ".roborak/context.md", "CONTRIBUTING.md")
MAX_CONTEXT_CHARS = 6000


def load_repo_context(repo: Path) -> str:
    """Pick up the repo's own conventions so the review matches house style."""
    for name in CONTEXT_FILES:
        path = repo / name
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            continue
        if text:
            return text[:MAX_CONTEXT_CHARS]
    return ""
