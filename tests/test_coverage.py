"""The coverage summary beside the verdict: decided once, from ``ReviewResult``."""

from __future__ import annotations

import pytest

from roborak.core.coverage import coverage_for
from roborak.core.models import (
    ChangedFile,
    ChangeSet,
    InvestigationDecision,
    InvestigationReport,
    InvestigationStatus,
    OmissionReason,
    ReviewResult,
    ReviewStatus,
    VerificationReport,
    VerificationRun,
    VerificationStatus,
)


def five_files() -> ReviewResult:
    return ReviewResult(changeset=ChangeSet(files=[ChangedFile(path=p) for p in "abcde"]))


def test_a_full_review_is_complete():
    coverage = coverage_for(five_files())
    assert not coverage.partial
    assert coverage.has_changeset
    assert (coverage.changed_files, coverage.reviewed_files) == (5, 5)
    assert coverage.omitted == {}


def test_coverage_loss_makes_the_scope_partial_and_counts_reasons():
    result = five_files()
    result.add_omission("d", OmissionReason.CONTEXT_LIMIT)
    result.add_omission("d", OmissionReason.CONTEXT_LIMIT, unit_id="second-range")
    result.add_omission("e", OmissionReason.CHUNK_FAILED)
    coverage = coverage_for(result)
    assert coverage.partial
    assert coverage.reviewed_files == 3
    assert coverage.omitted == {OmissionReason.CONTEXT_LIMIT: 1, OmissionReason.CHUNK_FAILED: 1}


def test_deliberate_exclusions_do_not_cost_coverage():
    result = five_files()
    result.add_omission("a", OmissionReason.IGNORED)
    result.add_omission("b", OmissionReason.BINARY)
    coverage = coverage_for(result)
    assert not coverage.partial
    assert coverage.reviewed_files == 3
    assert coverage.excluded == {OmissionReason.IGNORED: 1, OmissionReason.BINARY: 1}


def test_skipped_files_without_a_recorded_reason_still_count_as_omitted():
    result = five_files()
    result.skipped_files = ["e"]
    coverage = coverage_for(result)
    assert coverage.partial
    assert coverage.omitted == {None: 1}
    assert coverage.reviewed_files == 4


def test_a_partial_run_with_no_omission_is_still_partial():
    result = five_files()
    result.status = ReviewStatus.PARTIAL
    coverage = coverage_for(result)
    assert coverage.partial
    assert coverage.omitted == {}


def test_no_changeset_has_no_scope():
    assert not coverage_for(ReviewResult()).has_changeset


@pytest.mark.parametrize(
    ("runs", "expected"),
    [
        ([VerificationStatus.PASSED], VerificationStatus.PASSED),
        ([VerificationStatus.PASSED, VerificationStatus.FAILED], VerificationStatus.FAILED),
        ([VerificationStatus.SKIPPED], VerificationStatus.SKIPPED),
        ([], VerificationStatus.SKIPPED),
    ],
)
def test_verification_reads_the_report_status(runs, expected):
    result = ReviewResult(
        verification=VerificationReport(
            runs=[
                VerificationRun(name="tests", command=["pytest"], status=status) for status in runs
            ]
        )
    )
    coverage = coverage_for(result)
    assert coverage.verification is expected
    assert coverage.verification_checks == len(runs)


def test_verification_that_was_never_configured_is_not_skipped():
    assert coverage_for(ReviewResult()).verification is None


def test_investigation_distinguishes_unavailable_from_unresolved():
    unavailable = ReviewResult(
        investigation=InvestigationReport(status=InvestigationStatus.UNAVAILABLE, candidates=2)
    )
    unresolved = ReviewResult(
        investigation=InvestigationReport(
            status=InvestigationStatus.COMPLETED,
            candidates=2,
            decisions=[
                InvestigationDecision(candidate="1", disposition="confirm"),
                InvestigationDecision(candidate="2"),
            ],
        )
    )
    assert coverage_for(unavailable).investigation is InvestigationStatus.UNAVAILABLE
    assert coverage_for(unavailable).unresolved == 0
    assert coverage_for(unresolved).investigation is InvestigationStatus.COMPLETED
    assert coverage_for(unresolved).unresolved == 1
    assert coverage_for(ReviewResult()).investigation is None
