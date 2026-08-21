"""The pipeline that turns a changeset into a review.

Deliberately linear: source -> filter -> compress -> static -> LLM -> validate.
Each step is independently testable and none reaches backwards.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from roborak.analysis import validator
from roborak.context.chunker import chunk, needs_chunking
from roborak.context.compressor import compress, filter_files
from roborak.core.config import Config
from roborak.core.models import (
    ChangedFile,
    ChangeSet,
    Finding,
    Issue,
    LLMCallUsage,
    OmissionReason,
    ReviewResult,
    ReviewStatus,
    Walkthrough,
)
from roborak.core.severity import Kind
from roborak.llm.client import LLMClient, LLMError, LLMResponse
from roborak.llm.parser import (
    ParseError,
    parse_findings,
    parse_requirement_evidence,
    parse_walkthrough,
)
from roborak.llm.prompt import (
    build_ask_prompt,
    build_describe_prompt,
    build_improve_prompt,
    build_requirement_reducer_prompt,
    build_review_prompt,
    render_file_diff,
)
from roborak.rules.loader import load_rules, load_rules_at_ref
from roborak.rules.matcher import matching_rules, rules_for_prompt

log = logging.getLogger(__name__)


def render_for_prompt(file: ChangedFile) -> str:
    """What the compressor and chunker measure -- the same text the model will see."""
    return render_file_diff(file)


@dataclass
class Reviewer:
    config: Config
    repo: Path
    llm: LLMClient | None = None
    """``None`` runs the static-analysis-only path (``--no-llm``)."""

    static_findings: list[Finding] = field(default_factory=list)
    issue: Issue | None = None
    """What the change is supposed to achieve, when ``--issue`` supplied one."""

    _rules: list[object] | None = field(default=None, repr=False)
    _rules_key: str | None = field(default=None, repr=False)
    _usage: list[LLMCallUsage] = field(default_factory=list, repr=False)
    _contexts: dict[str, str] = field(default_factory=dict, repr=False)

    def rules_for(self, changeset: ChangeSet) -> list[dict[str, str]]:
        """The team's own rules that apply to this change, ready for the prompt."""
        reference = changeset.base_sha or changeset.base_ref or ""
        if self._rules is None or self._rules_key != reference:
            trusted = (
                load_rules_at_ref(self.repo, self.config.rules_dir, reference)
                if reference
                else None
            )
            self._rules = list(
                trusted if trusted is not None else load_rules(self.repo, self.config.rules_dir)
            )
            self._rules_key = reference
        matched = matching_rules(self._rules, changeset)  # type: ignore[arg-type]
        if matched:
            log.debug("%d of %d rules apply to this change", len(matched), len(self._rules))
        return rules_for_prompt(matched)

    def review(self, changeset: ChangeSet) -> ReviewResult:
        self._usage.clear()
        result = ReviewResult(
            changeset=changeset,
            model=self.config.model if self.llm else None,
            issue=self.issue,
        )

        if not self._prepare(changeset, result):
            return result

        findings = list(self.static_findings)
        if self.llm is not None:
            try:
                findings.extend(self._llm_findings(changeset, result))
            except (LLMError, ParseError) as exc:
                log.error("LLM review failed: %s", exc)
                result.errors.append(str(exc))
                result.status = ReviewStatus.FAILED

        result.findings = validator.validate(findings, changeset, self.config)
        self.apply_usage(result)
        return result

    def describe(self, changeset: ChangeSet) -> ReviewResult:
        """Produce a walkthrough instead of findings."""
        self._usage.clear()
        result = ReviewResult(changeset=changeset, model=self.config.model, issue=self.issue)
        if not self._prepare(changeset, result) or self.llm is None:
            return result

        try:
            # `describe` compresses the real changeset on purpose: the walkthrough
            # is the whole output, so the files it could not cover belong in the
            # report's own skipped list.
            result.walkthrough = self._walkthrough_on(changeset)
        except (LLMError, ParseError) as exc:
            log.error("describe failed: %s", exc)
            result.errors.append(str(exc))
            result.status = ReviewStatus.FAILED
        result.skipped_files = list(changeset.omitted_files)
        self.apply_usage(result)
        return result

    def walkthrough(self, changeset: ChangeSet) -> Walkthrough | None:
        """The overview pass that accompanies a review, or ``None`` if it failed.

        Deliberately non-fatal, and deliberately working on a copy. A review
        without an overview is still a review, so a failure here is logged rather
        than recorded in ``result.errors`` -- which would turn a clean review into
        a non-zero exit. The copy matters just as much: ``compress`` mutates, and
        shrinking the changeset the findings were anchored against would corrupt
        every line number downstream.
        """
        if self.llm is None:
            return None
        try:
            return self._walkthrough_on(changeset.model_copy(deep=True))
        except (LLMError, ParseError) as exc:
            log.warning("overview pass failed; reporting findings without one: %s", exc)
            return None

    def _walkthrough_on(self, changeset: ChangeSet) -> Walkthrough | None:
        """One describe call over ``changeset``, which this *will* compress."""
        assert self.llm is not None

        # A walkthrough is inherently one call, so an oversized change is
        # compressed rather than split: a walkthrough of half a change is worse
        # than one that says which files it could not cover.
        compress(
            changeset, self.llm.context_budget, self.llm.count_tokens, render=render_for_prompt
        )
        prompt = build_describe_prompt(
            changeset,
            self.config,
            repo_context=load_repo_context(self.repo, changeset.base_sha or changeset.base_ref),
            issue=self.issue,
        )
        response = self._complete("walkthrough", prompt.system, prompt.user)
        return parse_walkthrough(response.text)

    def improve(self, changeset: ChangeSet) -> ReviewResult:
        """Suggestion-only mode: every finding carries committable code."""
        self._usage.clear()
        result = ReviewResult(changeset=changeset, model=self.config.model, issue=self.issue)
        if not self._prepare(changeset, result) or self.llm is None:
            return result

        prompt = build_improve_prompt(
            changeset,
            self.config,
            rules=self.rules_for(changeset),  # type: ignore[arg-type]
            repo_context=load_repo_context(self.repo, changeset.base_sha or changeset.base_ref),
            issue=self.issue,
        )
        try:
            response = self._complete("improve", prompt.system, prompt.user)
            findings = parse_findings(response.text, valid_files={f.path for f in changeset.files})
        except (LLMError, ParseError) as exc:
            log.error("improve failed: %s", exc)
            result.errors.append(str(exc))
            result.status = ReviewStatus.FAILED
            return result

        # A suggestion with no code to commit is not a suggestion.
        findings = [f for f in findings if f.suggestion]
        result.findings = validator.validate(findings, changeset, self.config)
        self.apply_usage(result)
        return result

    def ask(self, changeset: ChangeSet, question: str) -> str:
        """Answer a free-text question about the change."""
        self._usage.clear()
        if self.llm is None:
            raise LLMError("`ask` needs a model; it cannot run with --no-llm.")

        filter_files(changeset, self.config.ignore_paths)
        if changeset.is_empty:
            return "There are no changes to ask about."

        compress(
            changeset, self.llm.context_budget, self.llm.count_tokens, render=render_for_prompt
        )
        prompt = build_ask_prompt(
            changeset,
            question,
            repo_context=load_repo_context(self.repo, changeset.base_sha or changeset.base_ref),
            issue=self.issue,
        )
        return self._complete("ask", prompt.system, prompt.user).text.strip()

    def _prepare(self, changeset: ChangeSet, result: ReviewResult) -> bool:
        """Filter in place. Returns False when nothing is left to review.

        Note what this deliberately does *not* do: drop files to fit the context
        budget. Oversized changes are handled by reviewing them in several passes
        (see `_llm_findings`), which loses nothing. Compression is a last resort,
        applied per-pass only once the chunker has run out of passes.
        """
        original = list(changeset.files)
        filter_files(changeset, self.config.ignore_paths)
        kept = {file.path for file in changeset.files}
        for file in original:
            if file.path in kept:
                continue
            if file.patch_unavailable:
                result.add_omission(file.path, OmissionReason.FORGE_PATCH_UNAVAILABLE)
                result.errors.append(f"forge did not provide a reviewable patch for {file.path}")
            elif file.is_binary:
                result.add_omission(file.path, OmissionReason.BINARY)
            else:
                result.add_omission(file.path, OmissionReason.IGNORED)
        if changeset.is_empty:
            return False
        for path in changeset.omitted_files:
            result.add_omission(path, OmissionReason.CONTEXT_LIMIT)
        return True

    def _llm_findings(self, changeset: ChangeSet, result: ReviewResult) -> list[Finding]:
        """Review the change, in several passes when it will not fit in one."""
        assert self.llm is not None

        budget = self._diff_budget(changeset)
        if not needs_chunking(changeset, budget, self.llm.count_tokens, render_for_prompt):
            single_findings, _ = self._review_chunk(changeset, result, chunk_index=1)
            return single_findings

        chunks = chunk(changeset, budget, self.llm.count_tokens, render_for_prompt)
        log.info("reviewing in %d passes", len(chunks))

        findings: list[Finding] = []
        evidence: list[dict[str, str]] = []
        successful_passes = 0
        for index, piece in enumerate(chunks, start=1):
            # Record anything the chunker had to drop, so the report can say so.
            changeset.omitted_files.extend(piece.omitted_files)
            try:
                chunk_findings, chunk_evidence = self._review_chunk(
                    piece,
                    result,
                    chunk_index=index,
                    collect_requirement_evidence=self.issue is not None
                    and self.config.review.check_requirements,
                )
                findings.extend(chunk_findings)
                evidence.extend(chunk_evidence)
                successful_passes += 1
            except (LLMError, ParseError) as exc:
                # One failed pass must not discard the passes that worked.
                log.error("pass %d of %d failed: %s", index, len(chunks), exc)
                result.status = ReviewStatus.PARTIAL
                result.errors.append(f"review pass {index} of {len(chunks)} failed: {exc}")
                for file in piece.files:
                    result.add_omission(file.path, OmissionReason.CHUNK_FAILED, str(exc))
        for path in changeset.omitted_files:
            result.add_omission(path, OmissionReason.CONTEXT_LIMIT)
            message = f"context pass limit omitted {path}"
            if message not in result.errors:
                result.errors.append(message)
        if successful_passes == 0 and result.errors:
            result.status = ReviewStatus.FAILED
        if (
            self.issue is not None
            and self.config.review.check_requirements
            and result.status is ReviewStatus.COMPLETE
        ):
            try:
                prompt = build_requirement_reducer_prompt(
                    self.issue, evidence, [file.path for file in changeset.files]
                )
                response = self._complete("requirements", prompt.system, prompt.user)
                gaps = parse_findings(response.text, valid_files={f.path for f in changeset.files})
                findings.extend(finding for finding in gaps if finding.kind is Kind.REQUIREMENT_GAP)
            except (LLMError, ParseError) as exc:
                result.status = ReviewStatus.PARTIAL
                result.errors.append(f"requirement reducer failed: {exc}")
        return findings

    def _review_chunk(
        self,
        changeset: ChangeSet,
        result: ReviewResult,
        *,
        chunk_index: int,
        collect_requirement_evidence: bool = False,
    ) -> tuple[list[Finding], list[dict[str, str]]]:
        assert self.llm is not None
        prompt_changeset = changeset.model_copy(deep=True)
        prompt = build_review_prompt(
            prompt_changeset,
            self.config,
            rules=self.rules_for(prompt_changeset),  # type: ignore[arg-type]
            static_findings=self._static_for_prompt(prompt_changeset),
            repo_context=self._repo_context(prompt_changeset),
            issue=self.issue,
            collect_requirement_evidence=collect_requirement_evidence,
        )
        total = self.llm.count_tokens(f"{prompt.system}\n{prompt.user}")
        # Tiny budgets are useful deterministic test doubles; real configured
        # budgets are validated at >=1000 tokens and include prompt scaffolding.
        if self.llm.context_budget >= 1000 and total > self.llm.context_budget:
            diff_tokens = max(
                1,
                self.llm.count_tokens(
                    "\n".join(render_for_prompt(file) for file in prompt_changeset.files)
                ),
            )
            available = max(1, diff_tokens - (total - self.llm.context_budget) - 200)
            compress(
                prompt_changeset,
                available,
                self.llm.count_tokens,
                render=render_for_prompt,
            )
            for path in prompt_changeset.omitted_files:
                result.add_omission(path, OmissionReason.CONTEXT_LIMIT)
                error = f"context budget omitted {path}"
                if error not in result.errors:
                    result.errors.append(error)
            if prompt_changeset.is_empty:
                raise LLMError("no file in this pass fits the model context budget")
        prompt = build_review_prompt(
            prompt_changeset,
            self.config,
            rules=self.rules_for(prompt_changeset),  # type: ignore[arg-type]
            static_findings=self._static_for_prompt(prompt_changeset),
            repo_context=self._repo_context(prompt_changeset),
            # Every pass carries the issue: a requirement can be missed in any
            # chunk, and a chunk that omits it would report false gaps.
            issue=self.issue,
            collect_requirement_evidence=collect_requirement_evidence,
        )
        response = self._complete("review", prompt.system, prompt.user, chunk_index=chunk_index)
        log.debug("model returned %d chars", len(response.text))
        findings = parse_findings(
            response.text,
            valid_files={f.path for f in prompt_changeset.files},
        )
        if collect_requirement_evidence:
            findings = [finding for finding in findings if finding.kind is not Kind.REQUIREMENT_GAP]
        return (
            findings,
            parse_requirement_evidence(response.text) if collect_requirement_evidence else [],
        )

    def _complete(
        self, purpose: str, system: str, user: str, *, chunk_index: int | None = None
    ) -> LLMResponse:
        assert self.llm is not None
        response = self.llm.complete(system, user)
        self._usage.append(
            LLMCallUsage(
                purpose=purpose,
                model=response.model,
                prompt_tokens=response.prompt_tokens,
                completion_tokens=response.completion_tokens,
                latency_ms=response.latency_ms,
                cost_usd=response.cost_usd,
                chunk=chunk_index,
            )
        )
        return response

    def apply_usage(self, result: ReviewResult) -> None:
        """Synchronise calls made after ``review()``, such as the walkthrough."""
        result.usage = list(self._usage)
        result.tokens_used = sum(call.total_tokens for call in self._usage)
        result.models_used = list(dict.fromkeys(call.model for call in self._usage))

    def _repo_context(self, changeset: ChangeSet) -> str:
        reference = changeset.base_sha or changeset.base_ref or ""
        if reference not in self._contexts:
            self._contexts[reference] = load_repo_context(self.repo, reference or None)
        return self._contexts[reference]

    def _diff_budget(self, changeset: ChangeSet) -> int:
        """Reserve exact prompt scaffolding before grouping files into calls."""
        assert self.llm is not None
        if self.llm.context_budget < 1000:
            return self.llm.context_budget
        empty = changeset.model_copy(update={"files": []})
        prompt = build_review_prompt(
            empty,
            self.config,
            repo_context=self._repo_context(empty),
            issue=self.issue,
            collect_requirement_evidence=self.issue is not None
            and self.config.review.check_requirements,
        )
        overhead = self.llm.count_tokens(f"{prompt.system}\n{prompt.user}")
        return max(1, self.llm.context_budget - overhead - 200)

    def _static_for_prompt(self, changeset: ChangeSet) -> list[Finding]:
        """Only the static findings for files in this pass, so chunks stay focused."""
        if not (self.config.static.enabled and self.config.static.feed_to_llm):
            return []
        paths = {f.path for f in changeset.files}
        relevant = [f for f in self.static_findings if f.file in paths]
        return relevant[: self.config.static.max_findings_in_prompt]


# Files a repo uses to tell tools how it wants to be treated. Read in order; the
# first that exists wins, so a repo can point roborak at AGENTS.md without also
# feeding it a CLAUDE.md meant for a different agent.
CONTEXT_FILES = ("AGENTS.md", "CLAUDE.md", ".roborak/context.md", "CONTRIBUTING.md")
MAX_CONTEXT_CHARS = 6000


def load_repo_context(repo: Path, base_ref: str | None = None) -> str:
    """Pick up house style, preferring the trusted base revision when available."""
    for name in CONTEXT_FILES:
        if base_ref:
            try:
                shown = subprocess.run(
                    ["git", "show", f"{base_ref}:{name}"],
                    cwd=repo,
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
                if shown.returncode == 0 and (text := shown.stdout.strip()):
                    return text[:MAX_CONTEXT_CHARS]
            except (OSError, subprocess.TimeoutExpired):
                pass
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
