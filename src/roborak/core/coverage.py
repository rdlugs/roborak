"""How much of a review actually happened, computed once.

The verdict counts findings, and a finding count is only as good as the review
behind it: "no findings" over half the files, or over a suite that never ran, is a
different statement from "no findings" over all of them. The stage reports on
``ReviewResult`` already carry that distinction; this module reads it off them in
one place, so the report and the panel view phrase the same decision rather than
each deciding it. Like ``core.verdict``, nothing here imports ``render``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from roborak.core.models import (
    COVERAGE_LOSS_REASONS,
    InvestigationStatus,
    OmissionReason,
    ReviewResult,
    ReviewStatus,
    VerificationStatus,
)


@dataclass(frozen=True)
class Coverage:
    """What was reviewed, what was verified, and what investigation could settle.

    ``None`` on a stage means the stage is absent from the result -- not configured,
    switched off, or never asked -- which is a different statement from a report
    saying it was attempted and skipped or unavailable.
    """

    changed_files: int = 0
    """Every changed file roborak knew about, including the ones filtering and the
    context budget removed from ``changeset.files`` before the prompt was built."""

    reviewed_files: int = 0
    omitted: dict[OmissionReason | None, int] = field(default_factory=dict)
    """Files the review did not read, at a cost to coverage, by reason. ``None``
    counts files only known through ``skipped_files`` or ``omitted_files``, which
    carry no reason."""

    excluded: dict[OmissionReason, int] = field(default_factory=dict)
    """Files left out by design -- ignored, binary, empty -- by reason."""

    incomplete: bool = False
    """The same condition ``core.verdict.gate_for`` reads as inconclusive, plus any
    omission that cost coverage, so "complete" never sits beside a failed run."""

    failed: bool = False
    has_changeset: bool = False

    verification: VerificationStatus | None = None
    verification_checks: int = 0

    investigation: InvestigationStatus | None = None
    candidates: int = 0
    unresolved: int = 0

    @property
    def omitted_files(self) -> int:
        return sum(self.omitted.values())

    @property
    def excluded_files(self) -> int:
        return sum(self.excluded.values())


def coverage_for(result: ReviewResult) -> Coverage:
    """The coverage of ``result``, as every human-readable surface states it."""
    omitted: dict[OmissionReason | None, int] = {}
    omitted_paths: set[str] = set()
    for item in result.coverage:
        if item.reason in COVERAGE_LOSS_REASONS and item.path not in omitted_paths:
            omitted_paths.add(item.path)
            omitted[item.reason] = omitted.get(item.reason, 0) + 1
    unreasoned = [*result.skipped_files]
    if result.changeset is not None:
        unreasoned += result.changeset.omitted_files
    for path in unreasoned:
        if path not in omitted_paths:
            omitted_paths.add(path)
            omitted[None] = omitted.get(None, 0) + 1

    excluded: dict[OmissionReason, int] = {}
    excluded_paths: set[str] = set()
    for item in result.coverage:
        if item.path in omitted_paths or item.path in excluded_paths:
            continue
        excluded_paths.add(item.path)
        excluded[item.reason] = excluded.get(item.reason, 0) + 1

    # Filtering and compression remove files from `changeset.files` as they record
    # them, so the denominator is every path any of them mentions, not what is left.
    changeset = result.changeset
    present = [file.path for file in changeset.files] if changeset is not None else []
    known = set(present) | omitted_paths | excluded_paths
    skipped = omitted_paths | excluded_paths
    reviewed = sum(1 for path in present if path not in skipped)

    verification = result.verification
    investigation = result.investigation
    return Coverage(
        changed_files=len(known),
        reviewed_files=reviewed,
        omitted=omitted,
        excluded=excluded,
        incomplete=(
            result.status is not ReviewStatus.COMPLETE or bool(result.errors) or bool(omitted)
        ),
        # Mirrors `render.markdown._completion_note`: partial is its own answer, and
        # anything else short of a clean run is a failure.
        failed=result.status is not ReviewStatus.PARTIAL
        and (bool(result.errors) or result.status is not ReviewStatus.COMPLETE),
        has_changeset=changeset is not None and bool(known),
        verification=verification.status if verification is not None else None,
        verification_checks=len(verification.runs) if verification is not None else 0,
        investigation=investigation.status if investigation is not None else None,
        candidates=investigation.candidates if investigation is not None else 0,
        unresolved=investigation.unresolved if investigation is not None else 0,
    )
