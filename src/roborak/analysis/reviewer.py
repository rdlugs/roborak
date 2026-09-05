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
    contract_contexts,
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
    InvestigationReport,
    InvestigationStatus,
    Issue,
    LLMCallUsage,
    OmissionReason,
    ReviewResult,
    ReviewStatus,
    SupplyChainReport,
    VerificationReport,
    Walkthrough,
)
from roborak.core.severity import Kind
from roborak.investigate.runner import investigate
from roborak.llm.client import LLMClient, LLMError, LLMResponse
from roborak.llm.parser import (
    ParseError,
    parse_compatibility_evidence,
    parse_findings,
    parse_requirement_evidence,
    parse_walkthrough,
)
from roborak.llm.prompt import (
    RenderedPrompt,
    build_ask_prompt,
    build_describe_prompt,
    build_improve_prompt,
    build_reconciliation_prompt,
    build_review_prompt,
    render_file_diff,
)
from roborak.rules.loader import load_rules, load_rules_at_ref
from roborak.rules.matcher import matching_rules, rules_for_prompt
from roborak.supply.prompt import for_prompt as supply_chain_for_prompt

log = logging.getLogger(__name__)

MAX_RECONCILIATION_EVIDENCE = 60
"""How many entries of each evidence kind reach the reducer. Evidence repeats across
passes long before it runs out; past this point the extra entries crowd the prompt
rather than adding a place to look."""


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
    verification: VerificationReport | None = None
    """What the project's own checks said, when the CLI ran them. ``None`` means
    the stage never ran, which is not the same as a report saying it was skipped."""

    supply_chain: SupplyChainReport | None = None
    """What the change does to dependencies and infrastructure, when the CLI
    analysed it. Computed there rather than here because it needs the changeset
    *before* ``ignore_paths`` removes every lockfile from it."""

    issue: Issue | None = None
    """What the change is supposed to achieve, when ``--issue`` supplied one."""

    forge_token: str | None = None
    """Credentials for the forge the change came from, when the CLI had any.

    Used by one stage only: the blast-radius pass, to fetch a temporary checkout
    of a private merge or pull request whose head this machine has never seen. It
    is never needed when the local remote already authenticates."""

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
        """One change in, one result out: static findings, the model's, and how to explain both."""
        self._usage.clear()
        result = ReviewResult(
            changeset=changeset,
            model=self.config.model if self.llm else None,
            issue=self.issue,
            # Set here rather than after `_prepare`: the stage already ran, and a
            # change whose files all filtered out still has an execution record
            # that a dropped report would silently turn into "never configured".
            verification=self.verification,
            # Same reasoning, and load-bearing rather than defensive here: a change
            # that only touches lockfiles filters to nothing, and dropping the
            # report would leave the one review that most needs it with no
            # dependency analysis at all.
            supply_chain=self.supply_chain,
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

        result.investigation = self._investigate(findings, changeset)

        result.findings = validator.validate(findings, changeset, self.config)
        self.apply_usage(result)
        return result

    def _investigate(
        self, findings: list[Finding], changeset: ChangeSet
    ) -> InvestigationReport | None:
        """Settle what the evidence policy is about to judge, before it judges it.

        Runs here rather than after validation because ``enforce_evidence`` is the
        gate this stage exists to feed: a candidate that comes back proven keeps
        its severity, and one that comes back disproved never reaches a reader.
        Non-fatal by construction, like the overview pass -- a review whose
        investigation broke is still a review.
        """
        if self.llm is None or not self.config.review.investigate.enabled:
            return None
        try:
            _, report = investigate(
                findings,
                changeset,
                repo=self.repo,
                config=self.config.review.investigate,
                complete=lambda system, user: self._complete("investigation", system, user).text,
            )
        except Exception as exc:  # noqa: BLE001 - evidence is optional; a review is not
            log.warning("investigation stage failed; findings stand as they were: %s", exc)
            return InvestigationReport(
                status=InvestigationStatus.ERRORED,
                notes=[f"investigation did not run: {exc}"],
            )
        return report

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
            return impact.analyse(
                changeset, self.repo, self.config.impact, forge_token=self.forge_token
            )
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
            self._diff_budget(changeset, carries_contracts=True),
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
        planned_by_path = {file.path: file for file in plan.review.files}

        def eligible_contracts() -> list[ContractContext]:
            reviewed_paths = {file.path for file in plan.review.files if file.reviewed}
            return [contract for contract in plan.contracts if contract.path in reviewed_paths]

        for index, piece in enumerate(chunks, start=1):
            changeset.omitted_files.extend(piece.omitted_files)
            under_review = {file.path for file in piece.files}
            carried_contracts = [
                contract
                for contract in eligible_contracts()
                # A split file is still primary diff in the pass holding its later
                # fragments. Carrying it there would hand the model a summary of the
                # very lines it is reviewing, under an instruction not to report on
                # them -- so it is carried only once the file is behind us.
                if contract.path not in under_review
                and (source_chunk := planned_by_path[contract.path].chunk) is not None
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
                    planned = planned_by_path.get(file.path)
                    if planned is not None:
                        planned.reviewed = False
                        planned.chunk = None
        for path in changeset.omitted_files:
            result.add_omission(path, OmissionReason.CONTEXT_LIMIT)
            message = f"context pass limit omitted {path}"
            if message not in result.errors:
                result.errors.append(message)
        if successful_passes == 0 and result.errors:
            result.status = ReviewStatus.FAILED
        reviewed_contracts = eligible_contracts()
        should_reconcile = bool(reviewed_contracts) or (
            self.issue is not None and self.config.review.check_requirements
        )
        if should_reconcile and result.status is ReviewStatus.COMPLETE:
            try:
                prompt = self._reconciliation_prompt(
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

    def _reconciliation_prompt(
        self,
        *,
        requirement_evidence: list[dict[str, str]],
        compatibility_evidence: list[dict[str, str]],
        contracts: list[ContractContext],
        files: list[str],
    ) -> RenderedPrompt:
        """Fit the reducer's own prompt inside the budget before it is sent.

        Every pass contributes evidence, so a change reviewed in many passes can
        accumulate more than the reducer can read -- and the reducer has no chunked
        fallback: one over-budget call turns a complete review into a PARTIAL one.
        Evidence is dropped from the longer list first, so both kinds survive as far
        as the budget allows; a change whose contracts and paths alone overflow the
        budget sheds those too, because an over-budget prompt reconciles nothing.
        """
        assert self.llm is not None
        requirements = requirement_evidence[:MAX_RECONCILIATION_EVIDENCE]
        compatibility = compatibility_evidence[:MAX_RECONCILIATION_EVIDENCE]
        dropped = (len(requirement_evidence) - len(requirements)) + (
            len(compatibility_evidence) - len(compatibility)
        )
        dropped_contracts = 0
        dropped_files = 0
        while True:
            prompt = build_reconciliation_prompt(
                issue=(
                    self.issue
                    if self.issue is not None and self.config.review.check_requirements
                    else None
                ),
                requirement_evidence=requirements,
                compatibility_evidence=compatibility,
                contracts=contracts,
                files=files,
            )
            if self.llm.context_budget < 1000:
                break
            total = self.llm.count_tokens(f"{prompt.system}\n{prompt.user}")
            if total <= self.llm.context_budget:
                break
            if requirements or compatibility:
                if len(compatibility) >= len(requirements):
                    dropped += len(compatibility) - len(compatibility) // 2
                    compatibility = compatibility[: len(compatibility) // 2]
                else:
                    dropped += len(requirements) - len(requirements) // 2
                    requirements = requirements[: len(requirements) // 2]
            elif contracts:
                # Contracts arrive in review-priority order, so the ones a mismatch is
                # most likely to involve are the ones kept.
                dropped_contracts += len(contracts) - len(contracts) // 2
                contracts = contracts[: len(contracts) // 2]
            elif len(files) > 1:
                dropped_files += len(files) - max(1, len(files) // 2)
                files = files[: max(1, len(files) // 2)]
            else:
                # Only the issue body is left, and dropping it would defeat the very
                # requirement check the reducer was called for.
                break
        if dropped:
            log.warning("reconciliation dropped %d evidence entries to fit the budget", dropped)
        if dropped_contracts or dropped_files:
            log.warning(
                "reconciliation dropped %d contracts and %d changed paths to fit the budget",
                dropped_contracts,
                dropped_files,
            )
        return prompt

    def _review_chunk(
        self,
        changeset: ChangeSet,
        result: ReviewResult,
        *,
        chunk_index: int,
        contract_contexts: list[ContractContext] | None = None,
        collect_reconciliation_evidence: bool = False,
    ) -> tuple[list[Finding], list[dict[str, str]], list[dict[str, str]]]:
        """One model pass over one chunk: the findings, and the evidence to reconcile them."""
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
            verification=self._verification_for_prompt(),
            supply_chain=self._supply_chain_for_prompt(),
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
            verification=self._verification_for_prompt(),
            supply_chain=self._supply_chain_for_prompt(),
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

    def _diff_budget(self, changeset: ChangeSet, *, carries_contracts: bool = False) -> int:
        """Reserve exact prompt scaffolding before grouping files into calls.

        ``carries_contracts`` is for the chunked path, where every pass after the
        first is handed the contracts earlier passes established. That section is
        absent from the prompt measured here, so without reserving it the planner
        fills a chunk to the brim and the carried contracts push the pass over the
        limit -- compressing away a file the plan had promised to review. A single
        pass carries no contracts and reserves nothing, so a change that fits whole
        is still reviewed whole.
        """
        assert self.llm is not None
        if self.llm.context_budget < 1000:
            return self.llm.context_budget
        empty = changeset.model_copy(update={"files": []})
        prompt = build_review_prompt(
            empty,
            self.config,
            repo_context=self._repo_context(empty),
            issue=self.issue,
            verification=self._verification_for_prompt(),
            contract_contexts=(
                contract_contexts(changeset.files, self._impact) if carries_contracts else None
            ),
            collect_reconciliation_evidence=True,
        )
        overhead = self.llm.count_tokens(f"{prompt.system}\n{prompt.user}")
        # The blast-radius section is reserved rather than measured: it is capped
        # before it is ever rendered, and reserving the ceiling up front is what
        # stops a large map from squeezing a changed file out of its own review.
        reserved = self.config.impact.token_budget if self._impact is not None else 0
        # The dependency delta is reserved on the same terms and, like the map, is
        # deliberately absent from the prompt measured above: it is capped by
        # `max_changes` before it is ever rendered, and measuring it here as well
        # as reserving it would charge the diff for it twice.
        # `for_prompt` is the same call the prompt builder makes, and it drops a
        # `nothing_relevant` report entirely -- so asking it, rather than only
        # whether the stage ran, keeps the diff from paying for a section that
        # renders as nothing.
        if supply_chain_for_prompt(self._supply_chain_for_prompt()) is not None:
            reserved += self.config.supply_chain.token_budget
        return max(1, self.llm.context_budget - overhead - reserved - 200)

    def _verification_for_prompt(self) -> VerificationReport | None:
        """The execution record, when the project wants the model to see it."""
        if not self.config.verification.feed_to_llm:
            return None
        return self.verification

    def _supply_chain_for_prompt(self) -> SupplyChainReport | None:
        """The dependency and infrastructure report, when the project wants it seen."""
        if not self.config.supply_chain.feed_to_llm:
            return None
        return self.supply_chain

    def _static_for_prompt(self, changeset: ChangeSet) -> list[Finding]:
        """Only the static findings for files in this pass, so chunks stay focused."""
        if not (self.config.static.enabled and self.config.static.feed_to_llm):
            return []
        paths = {f.path for f in changeset.files}
        relevant = [f for f in self.static_findings if f.file in paths]
        return relevant[: self.config.static.max_findings_in_prompt]


CONTEXT_FILES = ("AGENTS.md", "CLAUDE.md", ".roborak/context.md", "CONTRIBUTING.md")
MAX_CONTEXT_CHARS = 6000


def _read_context_file(repo: Path, name: str, base_ref: str | None) -> str:
    """One context file, from the trusted base revision when we can reach it."""
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
                return text
        except (OSError, subprocess.TimeoutExpired):
            pass
    path = repo / name
    if not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return ""


def load_repo_context(repo: Path, base_ref: str | None = None) -> str:
    """Pick up house style, preferring the trusted base revision when available."""
    for index, name in enumerate(CONTEXT_FILES):
        text = _read_context_file(repo, name, base_ref)
        if not text:
            continue
        # A pointer file (an AGENTS.md that only says "read CLAUDE.md") carries no
        # conventions of its own, so follow it rather than handing the model the sign.
        for pointed in CONTEXT_FILES[index + 1 :]:
            if pointed not in text:
                continue
            if pointed_text := _read_context_file(repo, pointed, base_ref):
                text = f"{text}\n\n{pointed_text}"
        return text[:MAX_CONTEXT_CHARS]
    return ""
