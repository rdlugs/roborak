"""The data model every stage of the pipeline speaks.

``ChangeSet`` is the universal IR: local git, GitLab, GitHub and plain paths all
normalise into it, so nothing downstream of a source needs to know where the code
came from. ``Finding`` is what every producer (static analysis, the LLM, custom
rules) emits and what every renderer and publisher consumes.
"""

from __future__ import annotations

import hashlib
import re
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field

from roborak.core.severity import Category, Effort, Evidence, Kind, Severity

ChangeType = Literal["added", "modified", "deleted", "renamed"]
Origin = Literal["local", "gitlab", "github", "paths"]


class ReviewStatus(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    FAILED = "failed"


class ReviewRole(StrEnum):
    """Why a changed file occupies its position in a multi-pass review."""

    CONTRACT = "contract"
    SCHEMA_CONFIG = "schema_config"
    IMPLEMENTATION = "implementation"
    CONSUMER = "consumer"
    TEST = "test"
    LOW_SIGNAL = "low_signal"


class ReviewPlanFile(BaseModel):
    """One file's semantic assignment in the bounded review plan."""

    path: str
    role: ReviewRole
    order: int = Field(ge=1)
    chunk: int | None = Field(default=None, ge=1)
    reviewed: bool = True


class ReviewPlan(BaseModel):
    """Explainable order and coverage for a multi-pass review."""

    files: list[ReviewPlanFile] = Field(default_factory=list)
    chunks: int = Field(default=0, ge=0)

    @property
    def omitted_roles(self) -> dict[ReviewRole, int]:
        counts: dict[ReviewRole, int] = {}
        for file in self.files:
            if not file.reviewed:
                counts[file.role] = counts.get(file.role, 0) + 1
        return counts


class OmissionReason(StrEnum):
    IGNORED = "ignored"
    BINARY = "binary"
    EMPTY_FILE = "empty_file"
    FORGE_PATCH_UNAVAILABLE = "forge_patch_unavailable"
    CONTEXT_LIMIT = "context_limit"
    CHUNK_FAILED = "chunk_failed"


class ReviewOmission(BaseModel):
    path: str
    reason: OmissionReason
    detail: str | None = None


class LLMCallUsage(BaseModel):
    purpose: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: int = 0
    cost_usd: float | None = None
    chunk: int | None = None

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class Hunk(BaseModel):
    """One ``@@`` block of a unified diff.

    ``line_map`` is the reason this class exists: forges anchor inline comments by
    position within the diff, not by file line number, so we record the mapping
    while we still have the diff in front of us.
    """

    old_start: int
    old_lines: int
    new_start: int
    new_lines: int
    header: str = ""
    content: str
    line_map: dict[int, int] = Field(default_factory=dict)
    """new-file line number -> position within the file's diff body (1-based)."""

    added_lines: set[int] = Field(default_factory=set)
    """new-file line numbers this hunk actually adds or modifies."""

    @property
    def new_end(self) -> int:
        return self.new_start + max(self.new_lines, 1) - 1

    def contains_new_line(self, lineno: int) -> bool:
        return self.new_start <= lineno <= self.new_end


class ChangedFile(BaseModel):
    path: str
    previous_path: str | None = None
    change_type: ChangeType = "modified"
    language: str | None = None
    hunks: list[Hunk] = Field(default_factory=list)
    new_content: str | None = None
    """Full post-change file body. Populated for path review and for AST context."""

    is_binary: bool = False
    zero_byte: bool = False
    """Both sides of the change are empty -- a placeholder such as ``.gitkeep``.

    The forge supplies no patch for one, and reconstruction has nothing to
    reconstruct, so it arrives looking exactly like a file whose patch could not
    be recovered. It is not: there is nothing here to review, and nothing failed.
    Kept apart from ``patch_unavailable`` so that a benign omission does not make
    an otherwise complete review inconclusive."""

    patch_unavailable: bool = False
    """The forge omitted a text patch and reconstruction did not recover it."""

    @property
    def added_lines(self) -> set[int]:
        return {line for hunk in self.hunks for line in hunk.added_lines}

    def hunk_for_line(self, lineno: int) -> Hunk | None:
        return next((h for h in self.hunks if h.contains_new_line(lineno)), None)

    def diff_position(self, lineno: int) -> int | None:
        for hunk in self.hunks:
            if lineno in hunk.line_map:
                return hunk.line_map[lineno]
        return None


class ForgeRef(BaseModel):
    """Everything needed to post a review back to where the diff came from."""

    provider: Literal["gitlab", "github"]
    host: str
    project: str
    """GitLab project path or numeric id; ``owner/repo`` for GitHub."""

    number: int
    """MR iid or PR number."""

    base_sha: str | None = None
    start_sha: str | None = None
    head_sha: str | None = None
    web_url: str | None = None


class Issue(BaseModel):
    """A tracker issue the change is meant to solve.

    Carried alongside the ``ChangeSet`` rather than inside it: a ``ChangeSet``
    describes where the code came from, and the issue describes what it was
    supposed to achieve. Keeping them apart means a source never has to know
    whether an issue was supplied.
    """

    provider: Literal["gitlab", "github"]
    host: str
    project: str
    number: int
    title: str = ""
    body: str = ""
    labels: list[str] = Field(default_factory=list)
    state: str = ""
    web_url: str | None = None
    comments: list[str] = Field(default_factory=list)
    """Human discussion, oldest first. System notes are dropped at the source."""

    @property
    def reference(self) -> str:
        return f"#{self.number}"


class ReviewComment(BaseModel):
    """One eligible human comment from a merge or pull request discussion."""

    author: str = ""
    body: str
    path: str | None = None
    line: int | None = None
    created_at: str = ""


class ChangeSet(BaseModel):
    files: list[ChangedFile] = Field(default_factory=list)
    title: str | None = None
    description: str | None = None
    base_sha: str = ""
    head_sha: str = ""
    base_ref: str | None = None
    head_ref: str | None = None
    origin: Origin = "local"
    forge_ref: ForgeRef | None = None
    discussions: list[ReviewComment] = Field(default_factory=list)
    """Bounded, untrusted human discussion supplied as review context."""

    omitted_files: list[str] = Field(default_factory=list)
    """Files dropped by the compressor; surfaced in the report footer."""

    @property
    def is_empty(self) -> bool:
        return not self.files

    def file_by_path(self, path: str) -> ChangedFile | None:
        return next((f for f in self.files if f.path == path), None)

    @property
    def total_added_lines(self) -> int:
        return sum(len(f.added_lines) for f in self.files)

    @property
    def flow_digest(self) -> str:
        """Identity of the *shape* of the change, for reusing a published overview.

        Deliberately blind to what is inside a hunk: an amend or a rebase that
        moves no file and no hunk header tells the same story about the change,
        and re-narrating it costs a model call for nothing. What does move the
        digest is a file appearing, disappearing, being renamed, or its hunks
        landing somewhere else.
        """
        if self.is_empty:
            return ""
        parts: list[str] = []
        for file in sorted(self.files, key=lambda f: f.path):
            parts.append(f"{file.path}|{file.previous_path or ''}|{file.change_type}")
            parts.extend(
                f"{h.old_start},{h.old_lines},{h.new_start},{h.new_lines}" for h in file.hunks
            )
        return hashlib.sha256("\n".join(parts).encode()).hexdigest()[:16]


class Finding(BaseModel):
    """One reviewable observation, in new-file coordinates.

    Line numbers are *always* relative to the post-change file. Translation into a
    forge's position payload happens in ``roborak.publish``, never earlier.
    """

    file: str
    start_line: int
    end_line: int
    severity: Severity
    category: Category
    kind: Kind = Kind.POTENTIAL_ISSUE
    effort: Effort = Effort.MODERATE
    title: str
    body: str
    suggestion: str | None = None
    """Verbatim replacement for ``start_line``..``end_line``, if one is offered."""

    rule_id: str | None = None
    confidence: float = 0.8
    source: Literal["llm", "static", "rule"] = "llm"
    tool: str | None = None
    """Which static analyser produced this, when ``source == "static"``."""

    evidence: Evidence = Evidence.UNVERIFIED
    """What makes this finding true. ``confidence`` says how sure the model feels;
    this says what it can point at, which is what the blocker policy is judged on."""

    evidence_note: str = ""
    """One concise sentence naming the trigger and failure path, the violated
    contract, the reproduction, or the tool result. Empty means the claim stands on
    nothing, and ``roborak.analysis.validator`` treats it that way."""

    evidence_files: list[str] = Field(default_factory=list)
    """Files the evidence points at beyond the flagged one -- the caller on the
    other side of a contract, the test that reproduces it. The flagged file is
    already named by ``location``; repeating it here says nothing, so a reader
    can take every path listed as somewhere else to look."""

    def model_post_init(self, __context: object) -> None:
        if self.end_line < self.start_line:
            self.end_line = self.start_line
        # A tool ran and said so, which is evidence of a different kind from
        # reasoning. Defaulting here rather than in five adapters keeps a new one
        # from silently arriving unverified.
        if self.source == "static" and "evidence" not in self.model_fields_set:
            self.evidence = Evidence.STATIC_TOOL

    @property
    def fingerprint(self) -> str:
        """Stable identity across runs, so we never post the same comment twice.

        Deliberately excludes line numbers: unrelated edits above a finding shift
        its line without making it a new finding.
        """
        normalised = re.sub(r"\s+", " ", self.body.strip().lower())[:200]
        key = f"{self.file}|{self.category}|{self.rule_id or ''}|{normalised}"
        return hashlib.sha256(key.encode()).hexdigest()[:16]

    @property
    def fingerprint_v2(self) -> str:
        """A wording-resistant identity retained alongside the legacy body hash."""
        title = re.sub(r"[^a-z0-9]+", " ", self.title.lower()).strip()
        key = f"{self.file}|{self.category}|{self.kind}|{self.rule_id or self.tool or ''}|{title}"
        return hashlib.sha256(key.encode()).hexdigest()[:16]

    @property
    def location(self) -> str:
        if self.start_line == self.end_line:
            return f"{self.file}:{self.start_line}"
        return f"{self.file}:{self.start_line}-{self.end_line}"


class FileSummary(BaseModel):
    """One row of the walkthrough table."""

    path: str
    summary: str


class Walkthrough(BaseModel):
    """The ``describe`` output — a walkthrough of the change as a whole."""

    title: str | None = None
    overview: str = ""
    file_summaries: list[FileSummary] = Field(default_factory=list)
    sequence_diagram: str | None = None
    """Mermaid flowchart or sequence diagram source.

    The legacy field name remains part of the public result schema for compatibility.
    """

    labels: list[str] = Field(default_factory=list)
    estimated_effort: int | None = Field(default=None, ge=1, le=5)


class BoundaryKind(StrEnum):
    """What sort of contract a changed thing exposes to the rest of the repository.

    A function is not the only thing a change can break from a distance. A route
    string, an event name, a configuration key, an environment variable and a
    schema field are all names that something else depends on by spelling them the
    same way, and none of them is a symbol any parser will hand you.
    """

    SYMBOL = "symbol"
    EXPORT = "export"
    ROUTE = "route"
    EVENT = "event"
    CONFIG_KEY = "config_key"
    ENV_VAR = "env_var"
    SCHEMA_FIELD = "schema_field"


class Verification(StrEnum):
    """How hard the evidence behind a consumer list is.

    The distinction this exists to preserve: finding the name and finding a *use*
    of it are not the same claim, and only the second one can support containment.
    """

    PARSED = "parsed"
    """A parser confirmed the match is an identifier, not a comment or a string."""

    TEXTUAL = "textual"
    """The name was matched as text. Aliases and dynamic dispatch are invisible."""

    NONE = "none"
    """Nothing was searched."""


class ImpactStatus(StrEnum):
    """What the blast-radius analysis was able to establish."""

    CONTAINED = "contained"
    """Searched with a parser, and nothing outside the change uses this."""

    CONSUMERS_FOUND = "consumers_found"
    NO_REFERENCES_FOUND = "no_references_found"
    """Nothing matched, but only as text -- which is not the same as contained."""

    UNSUPPORTED = "unsupported"
    """No parser for this language, so nothing could be seeded."""

    LIMITED = "limited"
    """Searched a checkout that may not hold exactly the code under review."""

    UNAVAILABLE = "unavailable"
    """There was nothing to search: a forge diff with no matching checkout."""

    NOT_APPLICABLE = "not_applicable"
    """There is no *unchanged* consumer to look for -- every file is under review."""


class ConsumerRelation(StrEnum):
    """How a consumer touches the changed thing."""

    CALL = "call"
    IMPORT = "import"
    """An import or a re-export: it carries the name onward rather than using it."""

    TEST = "test"
    CONFIG = "config"


class Consumer(BaseModel):
    """One place outside the change that names something the change touched."""

    path: str
    line: int
    relation: ConsumerRelation = ConsumerRelation.CALL
    snippet: str = ""
    """A few lines around the reference, for the prompt. Empty when not collected."""


class ImpactNode(BaseModel):
    """One changed contract and what depends on it.

    ``changed symbol -> direct consumers -> affected boundary -> verification``,
    which is the whole shape of the analysis in one record.
    """

    name: str
    kind: BoundaryKind = BoundaryKind.SYMBOL
    file: str
    line: int = 1
    consumers: list[Consumer] = Field(default_factory=list)
    status: ImpactStatus = ImpactStatus.NO_REFERENCES_FOUND
    verification: Verification = Verification.NONE
    note: str = ""
    truncated: bool = False
    """More consumers exist than were kept; the list is the head of a longer one."""


class ImpactMap(BaseModel):
    """The blast radius of a change, as evidence rather than as prose.

    Deliberately structured. The walkthrough narrates a change file by file, which
    cannot answer the one question a reader of a clean review has -- whether the
    change is contained, or whether nobody was able to look. A map that carries its
    own ``status`` can say which of those two it is, on every surface, without the
    renderer having to guess.
    """

    nodes: list[ImpactNode] = Field(default_factory=list)
    status: ImpactStatus = ImpactStatus.UNAVAILABLE
    method: Literal["git-grep", "walk", "none"] = "none"
    truncated: bool = False
    notes: list[str] = Field(default_factory=list)
    """Every bound that bit and every limitation that applies, in plain words."""

    @property
    def consumer_count(self) -> int:
        return sum(len(node.consumers) for node in self.nodes)

    @property
    def searched(self) -> bool:
        """Whether anything was actually looked for."""
        return self.status not in {
            ImpactStatus.UNAVAILABLE,
            ImpactStatus.NOT_APPLICABLE,
            ImpactStatus.UNSUPPORTED,
        }


class AssetKind(StrEnum):
    """What sort of trust boundary a changed file sits on.

    Every one of these is a place where a diff can change what the project trusts
    without changing a line of application code: what gets installed, what runs in
    CI, what the container is allowed to do, what the cloud will permit.
    """

    DEPENDENCY_MANIFEST = "dependency_manifest"
    """A human-authored declaration of what the project depends on."""

    DEPENDENCY_LOCK = "dependency_lock"
    """The resolver's answer to a manifest: exact versions, sources, checksums."""

    CI_WORKFLOW = "ci_workflow"
    CONTAINER = "container"
    IAC = "iac"
    PACKAGE_MANAGER_CONFIG = "package_manager_config"
    """Where packages are fetched from and under what rules -- a registry override
    is a supply-chain change even when no dependency moves."""


class DependencyChangeKind(StrEnum):
    """What happened to one package between the base and the head revision.

    Ordered by how much a reviewer should care rather than alphabetically:
    where a package comes from and whether it is still verifiable are supply-chain
    questions, while a version bump is an ordinary one.
    """

    SOURCE_CHANGED = "source_changed"
    """The registry, git remote or path it resolves from is not the one it was."""

    INTEGRITY_LOST = "integrity_lost"
    """It had a checksum and no longer does. Nothing verifies what gets installed."""

    INTEGRITY_CHANGED = "integrity_changed"
    """Same name, same version, different checksum -- the artefact was replaced."""

    MANIFEST_LOCK_DRIFT = "manifest_lock_drift"
    """The manifest and the lockfile disagree, so the resolved tree is not the
    declared one and whatever installs next is not what was reviewed."""

    ADDED = "added"
    REMOVED = "removed"
    UPGRADED = "upgraded"
    DOWNGRADED = "downgraded"


class DependencyChange(BaseModel):
    """One package's movement, as a fact rather than as a diff line.

    This is the whole reason the stage exists: a lockfile is generated data that
    never reaches the model, so what the model gets is this -- the semantic
    content of the change, at a size that fits beside the diff.
    """

    ecosystem: str
    name: str
    kind: DependencyChangeKind
    old_version: str = ""
    new_version: str = ""
    old_source: str = ""
    new_source: str = ""
    direct: bool = False
    """Named by the manifest, rather than pulled in by something else. A transitive
    package appearing on its own is the more interesting of the two."""

    note: str = ""
    """Why this is worth a reader's attention, when the kind does not say it."""

    @property
    def display_version(self) -> str:
        """``1.2.3 → 1.3.0``, or the single version when it did not move.

        A source or integrity change usually leaves the version alone, and showing
        ``1.3.0 → 1.3.0`` there reads as a movement that did not happen -- the
        arrow is what tells a reader something moved, so it is spent only when
        something did.
        """
        if self.old_version and self.new_version and self.old_version != self.new_version:
            return f"{self.old_version} → {self.new_version}"
        return self.new_version or self.old_version


class ChangedAsset(BaseModel):
    """One changed file that sits on a trust boundary, and which boundary it is."""

    path: str
    kind: AssetKind


class SupplyChainStatus(StrEnum):
    """What the supply-chain stage was able to establish.

    ``nothing_relevant`` and ``unavailable`` are deliberately different answers.
    The first says the change does not touch a dependency, a workflow, a container
    or any infrastructure, so there was nothing to analyse; the second says there
    was and we could not read it. A reader deciding how much a clean review is
    worth has to be able to tell them apart.
    """

    ANALYSED = "analysed"
    NOTHING_RELEVANT = "nothing_relevant"
    UNSUPPORTED = "unsupported"
    """Assets changed, but in an ecosystem or format with no parser here."""

    UNAVAILABLE = "unavailable"
    """Assets changed and could not be read -- a diff that is not checked out, a
    revision that is not there."""


class SupplyChainReport(BaseModel):
    """What the change does to the project's dependencies and infrastructure.

    Structured for the same reason ``ImpactMap`` is: the question a reader of a
    clean review has here is not "what did the diff say" but "was anybody able to
    look", and only a report that carries its own ``status`` can answer that on
    every surface without the renderer having to guess.
    """

    status: SupplyChainStatus = SupplyChainStatus.NOTHING_RELEVANT
    assets: list[ChangedAsset] = Field(default_factory=list)
    changes: list[DependencyChange] = Field(default_factory=list)
    scanner_findings: list[Finding] = Field(default_factory=list)
    """Scanner-confirmed whole-asset findings with no meaningful inline anchor."""

    ecosystems: list[str] = Field(default_factory=list)
    """Ecosystems a parser actually read, for the reader who wants to know which
    half of a polyglot repository this report speaks for."""

    truncated: bool = False
    notes: list[str] = Field(default_factory=list)
    """Every bound that bit, every ecosystem without a parser, and every scanner
    that was not run, in plain words."""

    @property
    def analysed(self) -> bool:
        """Whether anything was actually parsed."""
        return self.status is SupplyChainStatus.ANALYSED

    def kinds(self) -> set[AssetKind]:
        """The boundaries this change actually touches.

        The prompt is gated on this: a change that only edits Terraform never pays
        for the npm checklist, and one that only bumps a dependency never pays for
        the container one.
        """
        return {asset.kind for asset in self.assets}

    @property
    def has_dependency_assets(self) -> bool:
        return bool(
            self.kinds()
            & {
                AssetKind.DEPENDENCY_MANIFEST,
                AssetKind.DEPENDENCY_LOCK,
                AssetKind.PACKAGE_MANAGER_CONFIG,
            }
        )


class VerificationStatus(StrEnum):
    """How one verification command ended.

    ``failed`` and ``errored`` are deliberately different answers. A non-zero exit
    from a suite that ran is a statement about the change; a missing executable or
    a sandbox that would not let the suite write its cache is a statement about
    this machine, and reporting the second as the first would blame an author for
    a broken runner.
    """

    PASSED = "passed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    ERRORED = "errored"
    SKIPPED = "skipped"


class VerificationScope(StrEnum):
    """How much of the suite a command covers."""

    TARGETED = "targeted"
    """Selected because it matches the files this change touched."""

    BROAD = "broad"
    """The wide check, run when the change crosses a shared boundary or when no
    targeted command matched it."""


class VerificationRun(BaseModel):
    """One command that was chosen, and what happened when it ran."""

    name: str
    command: list[str] = Field(default_factory=list)
    """The argv actually selected, before any sandbox prefix. Recorded so a reader
    can run the same check by hand, which is the whole point of citing one."""

    status: VerificationStatus = VerificationStatus.SKIPPED
    exit_code: int | None = None
    duration_ms: int = 0
    scope: VerificationScope = VerificationScope.TARGETED
    output: str = ""
    """The tail of the command's combined output, bounded by configuration."""

    truncated: bool = False
    note: str = ""
    """Why a run was skipped or errored, in words a reader can act on."""

    @property
    def executed(self) -> bool:
        """Whether this command actually ran.

        ``errored`` is not execution: it is how a command that could not be
        started -- a missing runner, a sandbox that refused -- is recorded, and
        counting it would let a review that ran nothing report an execution
        record behind it.
        """
        return self.status not in {VerificationStatus.SKIPPED, VerificationStatus.ERRORED}

    @property
    def display_command(self) -> str:
        """The command as a reader would type it, for a report cell or a log line."""
        return " ".join(self.command)


class VerificationReport(BaseModel):
    """What the verification stage selected, ran, and found.

    Present on a ``ReviewResult`` whenever the stage was asked to do something --
    including when it decided it must not run. A report that says ``skipped`` is
    the load-bearing case: a reader who cannot tell a suite that passed from one
    that was never started will read every clean review as a verified one.
    """

    runs: list[VerificationRun] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    """Every bound that bit and every reason a command did not run."""

    source: str = ""
    """Where the commands came from, e.g. ``base revision a1b2c3d``. Verification
    executes repository-authored argv, so the provenance travels with the result."""

    @property
    def executed(self) -> bool:
        """Whether anything ran, which is what separates a verified review from a quiet one."""
        return any(run.executed for run in self.runs)

    @property
    def status(self) -> VerificationStatus:
        """The worst thing that happened, which is what a verdict reads.

        Ordered by how much it should worry a reader rather than alphabetically:
        a failure outranks a timeout outranks a broken runner outranks a skip, and
        ``passed`` is only reachable when something actually ran and nothing else
        did anything worse.
        """
        for status in (
            VerificationStatus.FAILED,
            VerificationStatus.TIMED_OUT,
            VerificationStatus.ERRORED,
        ):
            if any(run.status is status for run in self.runs):
                return status
        if self.executed:
            return VerificationStatus.PASSED
        return VerificationStatus.SKIPPED

    @property
    def failing(self) -> list[VerificationRun]:
        """The runs a reader has to act on -- a failure or a timeout, never a skip."""
        return [
            run
            for run in self.runs
            if run.status in {VerificationStatus.FAILED, VerificationStatus.TIMED_OUT}
        ]


class InvestigationStatus(StrEnum):
    """How the investigation stage ended.

    ``skipped`` and ``unavailable`` are deliberately different answers. Skipped
    means the stage looked and found nothing worth the cost -- no candidate whose
    verdict another read would move. Unavailable means it wanted to look and could
    not, because the checkout in front of us is not the code under review. A reader
    who cannot tell those apart reads the second as a clean bill of health.
    """

    COMPLETED = "completed"
    PARTIAL = "partial"
    """Some candidates were settled and some ran out of budget, rounds, or luck."""

    SKIPPED = "skipped"
    UNAVAILABLE = "unavailable"
    ERRORED = "errored"


class InvestigationOutcome(StrEnum):
    """What one requested operation produced."""

    OK = "ok"
    REFUSED = "refused"
    """The request was rejected before any I/O -- an escaping path, an unknown
    operation, a budget already spent. The refusal is recorded rather than
    silently dropped, because a model that never learns its request was refused
    will keep making it."""

    ERRORED = "errored"
    EMPTY = "empty"
    """The operation ran and found nothing, which is an answer and not a failure."""


class InvestigationOperation(BaseModel):
    """One read or search the model asked for, and what came back."""

    tool: str
    arguments: dict[str, str] = Field(default_factory=dict)
    """The request as roborak validated it, not as the model phrased it."""

    outcome: InvestigationOutcome = InvestigationOutcome.OK
    round_index: int = 1
    result: str = ""
    """The bounded, sanitised result. Untrusted repository content."""

    truncated: bool = False
    note: str = ""
    """Why an operation was refused or errored, in words a reader can act on."""

    @property
    def display_request(self) -> str:
        """The request as a reader would describe it, for a report row."""
        arguments = ", ".join(f"{key}={value}" for key, value in sorted(self.arguments.items()))
        return f"{self.tool}({arguments})"


class InvestigationDecision(BaseModel):
    """What the model concluded about one candidate once it had looked."""

    candidate: str
    """The id roborak assigned. A decision naming anything else is discarded."""

    disposition: Literal["confirm", "revise", "drop", "unresolved"] = "unresolved"
    """``unresolved`` is the default for a reason: a candidate the stage could not
    settle keeps whatever it arrived with, and is never confirmed or dropped by
    omission."""

    location: str = ""
    """Where the candidate ended up, for the report. Empty until it is settled."""

    title: str = ""
    rationale: str = ""
    """Why the model landed where it did, grounded in what it read."""


class InvestigationReport(BaseModel):
    """What the investigation stage selected, asked, and concluded.

    Present on a ``ReviewResult`` whenever the stage was asked to do something --
    including when it decided it must not run. Absent means it was switched off;
    a report saying ``skipped`` means it ran and had nothing to settle. Those are
    different claims and a reader acts differently on each.
    """

    status: InvestigationStatus = InvestigationStatus.SKIPPED
    operations: list[InvestigationOperation] = Field(default_factory=list)
    decisions: list[InvestigationDecision] = Field(default_factory=list)
    rounds: int = 0
    candidates: int = 0
    """How many findings were put to the stage, before any of them were settled."""

    notes: list[str] = Field(default_factory=list)
    """Every bound that bit and every reason an operation did not run."""

    @property
    def executed(self) -> bool:
        """Whether the stage actually gathered anything.

        A refusal is not execution: it is how a request that never reached the
        repository is recorded, and counting it would let an investigation that
        read nothing report evidence behind it.
        """
        return any(op.outcome is not InvestigationOutcome.REFUSED for op in self.operations)

    @property
    def settled(self) -> list[InvestigationDecision]:
        """The decisions a reader has to act on -- never an unresolved candidate."""
        return [d for d in self.decisions if d.disposition != "unresolved"]

    @property
    def unresolved(self) -> int:
        """Candidates the stage could not settle, which stay exactly as they were."""
        return len(self.decisions) - len(self.settled)


class ReviewResult(BaseModel):
    """Everything a review produced, ready for any renderer or publisher."""

    findings: list[Finding] = Field(default_factory=list)
    walkthrough: Walkthrough | None = None
    changeset: ChangeSet | None = None
    model: str | None = None
    issue: Issue | None = None
    """The issue this review was judged against, when ``--issue`` was given."""

    impact: ImpactMap | None = None
    """What the change reaches beyond the lines it touched.

    ``None`` means the stage never ran -- ``describe``, ``--no-llm``, or the
    analysis switched off -- which is a different statement from an ``ImpactMap``
    whose status is ``unavailable``. One says nobody asked; the other says we asked
    and could not find out, and a reader deciding whether to trust a clean review
    needs to be able to tell them apart."""

    review_plan: ReviewPlan | None = None
    """Semantic order and pass assignment, present when the diff was chunked."""

    verification: VerificationReport | None = None
    """What the project's own tests said about this change.

    ``None`` means the stage never ran -- nothing was configured, or it was
    switched off -- which is a different statement from a report whose status is
    ``skipped``. One says nobody asked for verification; the other says it was
    asked for and refused, and a reader deciding how much a clean review is worth
    has to be able to tell them apart."""

    supply_chain: SupplyChainReport | None = None
    """What the change does to dependencies, CI, containers and infrastructure.

    ``None`` means the stage never ran -- ``describe``, the analysis switched off,
    a command that does not do supply-chain work -- which is a different statement
    from a report whose status is ``nothing_relevant``. One says nobody looked; the
    other says we looked and this change does not touch any of it."""

    investigation: InvestigationReport | None = None
    """What the bounded investigation stage asked of the repository and concluded.

    ``None`` means the stage never ran -- ``--no-investigate``, no model, no
    candidate worth the call -- which is a different statement from a report whose
    status is ``unavailable``. One says nobody looked; the other says we wanted to
    and the checkout in front of us was not the code under review."""

    tokens_used: int = 0
    status: ReviewStatus = ReviewStatus.COMPLETE
    coverage: list[ReviewOmission] = Field(default_factory=list)
    models_used: list[str] = Field(default_factory=list)
    usage: list[LLMCallUsage] = Field(default_factory=list)
    skipped_files: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)

    block_on: Severity | None = None
    """The severity floor this review's verdict is judged against.

    Recorded by the CLI so that every surface -- the report, the summary comment,
    the forge status -- can state the same verdict. ``markdown.render`` and the
    publishers are handed a ``ReviewResult`` and nothing else, and threading a
    flag through all of them would give each surface its own chance to disagree.
    ``None`` only while a review is still being assembled; see
    ``roborak.core.verdict.gate_for``."""

    block_on_explicit: bool = False
    """``block_on`` came from ``--fail-on`` rather than the configured default.
    Only an explicit floor moves the exit code, so the rendered block says which
    one it is rather than implying CI is gated when it is not."""

    def add_omission(self, path: str, reason: OmissionReason, detail: str | None = None) -> None:
        """Record a file the review did not read, and whether that cost it coverage.

        Only the reasons below leave the review inconclusive. An ignored, binary
        or empty file is a deliberate or empty-handed omission: it is worth
        listing, but the review still saw everything there was to see, so it stays
        ``COMPLETE`` and its verdict still means something.
        """
        omission = ReviewOmission(path=path, reason=reason, detail=detail)
        if omission not in self.coverage:
            self.coverage.append(omission)
        if reason in {
            OmissionReason.FORGE_PATCH_UNAVAILABLE,
            OmissionReason.CONTEXT_LIMIT,
            OmissionReason.CHUNK_FAILED,
        }:
            self.status = ReviewStatus.PARTIAL
            if path not in self.skipped_files:
                self.skipped_files.append(path)

    def add_usage(self, usage: LLMCallUsage) -> None:
        self.usage.append(usage)
        self.tokens_used += usage.total_tokens
        if usage.model not in self.models_used:
            self.models_used.append(usage.model)

    @property
    def counts_by_severity(self) -> dict[Severity, int]:
        counts = dict.fromkeys(Severity, 0)
        for finding in self.all_findings():
            counts[finding.severity] += 1
        return counts

    @property
    def has_blocking(self) -> bool:
        return any(f.severity is Severity.CRITICAL for f in self.all_findings())

    def all_findings(self) -> list[Finding]:
        """Inline findings plus scanner facts carried by the supply-chain report."""
        report_findings = self.supply_chain.scanner_findings if self.supply_chain else []
        return [*self.findings, *report_findings]

    def sorted_findings(self) -> list[Finding]:
        return sorted(
            self.findings,
            key=lambda f: (-f.severity.rank, f.file, f.start_line),
        )
