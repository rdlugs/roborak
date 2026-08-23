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

from roborak.core.severity import Category, Effort, Kind, Severity

ChangeType = Literal["added", "modified", "deleted", "renamed"]
Origin = Literal["local", "gitlab", "github", "paths"]


class ReviewStatus(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    FAILED = "failed"


class OmissionReason(StrEnum):
    IGNORED = "ignored"
    BINARY = "binary"
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

    def model_post_init(self, __context: object) -> None:
        if self.end_line < self.start_line:
            self.end_line = self.start_line

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


class ReviewResult(BaseModel):
    """Everything a review produced, ready for any renderer or publisher."""

    findings: list[Finding] = Field(default_factory=list)
    walkthrough: Walkthrough | None = None
    changeset: ChangeSet | None = None
    model: str | None = None
    issue: Issue | None = None
    """The issue this review was judged against, when ``--issue`` was given."""

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
        for finding in self.findings:
            counts[finding.severity] += 1
        return counts

    @property
    def has_blocking(self) -> bool:
        return any(f.severity is Severity.CRITICAL for f in self.findings)

    def sorted_findings(self) -> list[Finding]:
        return sorted(
            self.findings,
            key=lambda f: (-f.severity.rank, f.file, f.start_line),
        )
