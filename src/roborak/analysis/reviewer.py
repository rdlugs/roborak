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
from roborak.context import impact
from roborak.context.chunker import (
    ChunkStrategy,
    ContractContext,
    needs_chunking,
    plan_chunks,
)
from roborak.context.compressor import compress, filter_files
from roborak.core.config import Config
from roborak.core.models import (
    ChangedFile,
    ChangeSet,
    Finding,
    ImpactMap,
    ImpactStatus,
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
    parse_compatibility_evidence,
    parse_findings,
    parse_requirement_evidence,
    parse_walkthrough,
)
from roborak.llm.prompt import (
    build_ask_prompt,
    build_describe_prompt,
    build_improve_prompt,
    build_reconciliation_prompt,
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

    chunk_strategy: ChunkStrategy = "semantic"
    """The directory strategy exists only for quality comparisons in ``evals``."""

    static_findings: list[Finding] = field(default_factory=list)
    issue: Issue | None = None
    """What the change is supposed to achieve, when ``--issue`` supplied one."""

    _rules: list[object] | None = field(default=None, repr=False)
    _rules_key: str | None = field(default=None, repr=False)
    _usage: list[LLMCallUsage] = field(default_factory=list, repr=False)
    _contexts: dict[str, str] = field(default_factory=dict, repr=False)
    _impact: ImpactMap | None = field(default=None, repr=False)

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

        result.impact = self._impact = self._blast_radius(changeset)

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

    def _blast_radius(self, changeset: ChangeSet) -> ImpactMap | None:
        """What the change reaches, or ``None`` when nobody asked.

        Non-fatal by construction, the same way the overview pass is. A review
        that fell over because an optional context stage could not read the
        working tree would be a worse review than one without the map, so every
        failure here degrades to a note rather than to an error.
        """
        if not self.config.impact.enabled or self.llm is None:
            return None
        try:
            return impact.analyse(changeset, self.repo, self.config.impact)
        except Exception as exc:  # noqa: BLE001 - context is optional; a review is not
            log.warning("blast-radius analysis failed; reviewing without it: %s", exc)
            return ImpactMap(
                status=ImpactStatus.UNAVAILABLE,
                notes=[
                    "The blast-radius analysis did not complete, so no consumer was "
                    "searched for. The run log has the reason."
                ],
            )

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
            if file.zero_byte:
                result.add_omission(file.path, OmissionReason.EMPTY_FILE)
            elif file.patch_unavailable:
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
            single_findings, _, _ = self._review_chunk(changeset, result, chunk_index=1)
            return single_findings

        plan = plan_chunks(
            changeset,
            budget,
            self.llm.count_tokens,
            render_for_prompt,
            impact=self._impact,
            strategy=self.chunk_strategy,
        )
        chunks = plan.chunks
        result.review_plan = plan.review
        log.info("reviewing in %d passes", len(chunks))

        findings: list[Finding] = []
        requirement_evidence: list[dict[str, str]] = []
        compatibility_evidence: list[dict[str, str]] = []
        successful_passes = 0
        chunk_by_path = {file.path: file.chunk for file in plan.review.files}
        reviewed_paths = {file.path for file in plan.review.files if file.reviewed}
        reviewed_contracts = [
            contract for contract in plan.contracts if contract.path in reviewed_paths
        ]
        for index, piece in enumerate(chunks, start=1):
            changeset.omitted_files.extend(piece.omitted_files)
            carried_contracts = [
                contract
                for contract in reviewed_contracts
                if (source_chunk := chunk_by_path.get(contract.path)) is not None
                and source_chunk < index
            ]
            try:
                chunk_findings, chunk_requirements, chunk_compatibility = self._review_chunk(
                    piece,
                    result,
                    chunk_index=index,
                    contract_contexts=carried_contracts,
                    collect_reconciliation_evidence=True,
                )
                findings.extend(chunk_findings)
                requirement_evidence.extend(chunk_requirements)
                compatibility_evidence.extend(chunk_compatibility)
                successful_passes += 1
            except (LLMError, ParseError) as exc:
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
        should_reconcile = bool(reviewed_contracts) or (
            self.issue is not None and self.config.review.check_requirements
        )
        if should_reconcile and result.status is ReviewStatus.COMPLETE:
            try:
                prompt = build_reconciliation_prompt(
                    issue=(
                        self.issue
                        if self.issue is not None and self.config.review.check_requirements
                        else None
                    ),
                    requirement_evidence=requirement_evidence,
                    compatibility_evidence=compatibility_evidence,
                    contracts=reviewed_contracts,
                    files=[file.path for file in changeset.files],
                )
                response = self._complete("reconciliation", prompt.system, prompt.user)
                reconciled = parse_findings(
                    response.text, valid_files={file.path for file in changeset.files}
                )
                findings.extend(
                    finding
                    for finding in reconciled
                    if finding.kind is not Kind.REQUIREMENT_GAP
                    or (self.issue is not None and self.config.review.check_requirements)
                )
            except (LLMError, ParseError) as exc:
                result.status = ReviewStatus.PARTIAL
                result.errors.append(f"reconciliation failed: {exc}")
        return findings

    def _review_chunk(
        self,
        changeset: ChangeSet,
        result: ReviewResult,
        *,
        chunk_index: int,
        contract_contexts: list[ContractContext] | None = None,
        collect_reconciliation_evidence: bool = False,
    ) -> tuple[list[Finding], list[dict[str, str]], list[dict[str, str]]]:
        assert self.llm is not None
        prompt_changeset = changeset.model_copy(deep=True)
        prompt = build_review_prompt(
            prompt_changeset,
            self.config,
            rules=self.rules_for(prompt_changeset),  # type: ignore[arg-type]
            static_findings=self._static_for_prompt(prompt_changeset),
            repo_context=self._repo_context(prompt_changeset),
            issue=self.issue,
            impact=self._impact,
            contract_contexts=contract_contexts,
            collect_reconciliation_evidence=collect_reconciliation_evidence,
        )
        total = self.llm.count_tokens(f"{prompt.system}\n{prompt.user}")
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
                if result.review_plan is not None:
                    for planned in result.review_plan.files:
                        if planned.path == path:
                            planned.reviewed = False
                            planned.chunk = None
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
            issue=self.issue,
            impact=self._impact,
            contract_contexts=contract_contexts,
            collect_reconciliation_evidence=collect_reconciliation_evidence,
        )
        response = self._complete("review", prompt.system, prompt.user, chunk_index=chunk_index)
        log.debug("model returned %d chars", len(response.text))
        findings = parse_findings(
            response.text,
            valid_files={f.path for f in prompt_changeset.files},
        )
        if collect_reconciliation_evidence:
            findings = [finding for finding in findings if finding.kind is not Kind.REQUIREMENT_GAP]
        return (
            findings,
            parse_requirement_evidence(response.text) if collect_reconciliation_evidence else [],
            parse_compatibility_evidence(response.text) if collect_reconciliation_evidence else [],
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
            collect_reconciliation_evidence=True,
        )
        overhead = self.llm.count_tokens(f"{prompt.system}\n{prompt.user}")
        # The blast-radius section is reserved rather than measured: it is capped
        # before it is ever rendered, and reserving the ceiling up front is what
        # stops a large map from squeezing a changed file out of its own review.
        reserved = self.config.impact.token_budget if self._impact is not None else 0
        return max(1, self.llm.context_budget - overhead - reserved - 200)

    def _static_for_prompt(self, changeset: ChangeSet) -> list[Finding]:
        """Only the static findings for files in this pass, so chunks stay focused."""
        if not (self.config.static.enabled and self.config.static.feed_to_llm):
            return []
        paths = {f.path for f in changeset.files}
        relevant = [f for f in self.static_findings if f.file in paths]
        return relevant[: self.config.static.max_findings_in_prompt]


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
                    encoding="utf-8",
                    errors="replace",
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
