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


def drop(result: ReviewResult, path: str, reason: OmissionReason) -> None:
    """Omit ``path`` the way `Reviewer._prepare` and `compress` do: out of the files first."""
    assert result.changeset is not None
    result.changeset.files = [f for f in result.changeset.files if f.path != path]
    result.add_omission(path, reason)


def test_a_full_review_is_complete():
    coverage = coverage_for(five_files())
    assert not coverage.incomplete
    assert coverage.has_changeset
    assert (coverage.changed_files, coverage.reviewed_files) == (5, 5)
    assert coverage.omitted == {}


def test_coverage_loss_makes_the_scope_incomplete_and_counts_reasons():
    result = five_files()
    drop(result, "d", OmissionReason.CONTEXT_LIMIT)
    result.add_omission("d", OmissionReason.CONTEXT_LIMIT, unit_id="second-range")
    # A failed chunk leaves the file in the changeset; only its ranges went unread.
    result.add_omission("e", OmissionReason.CHUNK_FAILED)
    coverage = coverage_for(result)
    assert coverage.incomplete
    assert (coverage.changed_files, coverage.reviewed_files) == (5, 3)
    assert coverage.omitted == {OmissionReason.CONTEXT_LIMIT: 1, OmissionReason.CHUNK_FAILED: 1}


def test_files_removed_before_the_prompt_stay_in_the_denominator():
    """Three changed, one compressed away: "2 of 3", never "2 of 2"."""
    result = ReviewResult(changeset=ChangeSet(files=[ChangedFile(path=p) for p in "abc"]))
    assert result.changeset is not None
    result.changeset.files = result.changeset.files[:2]
    result.changeset.omitted_files = ["c"]
    coverage = coverage_for(result)
    assert (coverage.changed_files, coverage.reviewed_files) == (3, 2)
    assert coverage.omitted == {None: 1}
    assert coverage.incomplete


def test_deliberate_exclusions_do_not_cost_coverage():
    result = five_files()
    drop(result, "a", OmissionReason.IGNORED)
    drop(result, "b", OmissionReason.BINARY)
    coverage = coverage_for(result)
    assert not coverage.incomplete
    assert (coverage.changed_files, coverage.reviewed_files) == (5, 3)
    assert coverage.excluded == {OmissionReason.IGNORED: 1, OmissionReason.BINARY: 1}


def test_a_change_whose_every_file_was_filtered_still_has_a_scope():
    result = ReviewResult(changeset=ChangeSet(files=[ChangedFile(path=p) for p in "abc"]))
    drop(result, "a", OmissionReason.BINARY)
    drop(result, "b", OmissionReason.BINARY)
    drop(result, "c", OmissionReason.IGNORED)
    coverage = coverage_for(result)
    assert coverage.has_changeset
    assert (coverage.changed_files, coverage.reviewed_files) == (3, 0)
    assert not coverage.incomplete


def test_skipped_files_without_a_recorded_reason_still_count_as_omitted():
    result = five_files()
    result.skipped_files = ["e"]
    coverage = coverage_for(result)
    assert coverage.incomplete
    assert coverage.omitted == {None: 1}
    assert coverage.reviewed_files == 4


def test_a_partial_run_with_no_omission_is_incomplete_but_not_failed():
    result = five_files()
    result.status = ReviewStatus.PARTIAL
    coverage = coverage_for(result)
    assert coverage.incomplete
    assert not coverage.failed
    assert coverage.omitted == {}


@pytest.mark.parametrize(
    ("status", "errors"),
    [(ReviewStatus.FAILED, []), (ReviewStatus.COMPLETE, ["the model timed out"])],
)
def test_a_failed_run_is_never_complete(status, errors):
    result = five_files()
    result.status = status
    result.errors = errors
    coverage = coverage_for(result)
    assert coverage.incomplete
    assert coverage.failed


def test_no_changeset_has_no_scope():
    assert not coverage_for(ReviewResult()).has_changeset
    assert not coverage_for(ReviewResult(changeset=ChangeSet())).has_changeset


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
