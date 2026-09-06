"""The pass/fail verdict.

The point of ``core.verdict`` is that the report, the forge status and the exit
code cannot disagree, so the tests that matter most are the ones that pin those
three to each other.
"""

from __future__ import annotations

import pytest
import typer

from roborak.analysis import validator
from roborak.cli.shared import EXIT_ERROR, EXIT_FINDINGS, EXIT_OK, finish
from roborak.core.models import (
    CheckId,
    CheckOutcome,
    CheckResult,
    ChecksReport,
    Finding,
    ReviewResult,
    ReviewStatus,
)
from roborak.core.severity import Category, Enforcement, Kind, Severity
from roborak.core.verdict import Verdict, blocking_findings, gate_for

BAD_ERROR = (Enforcement.ERROR, CheckOutcome.FAILED)
BAD_WARNING = (Enforcement.WARNING, CheckOutcome.FAILED)


def finding(severity: Severity, file: str = "app/auth.py") -> Finding:
    return Finding(
        file=file,
        start_line=11,
        end_line=11,
        severity=severity,
        category=Category.SECURITY,
        title=f"A {severity} problem",
        body="Details.",
    )


def result(*severities: Severity, **kwargs) -> ReviewResult:
    return ReviewResult(findings=[finding(s) for s in severities], **kwargs)


@pytest.mark.parametrize(
    ("severities", "floor", "expected"),
    [
        ((), Severity.CRITICAL, Verdict.PASS),
        ((Severity.MINOR,), Severity.CRITICAL, Verdict.PASS),
        ((Severity.CRITICAL,), Severity.CRITICAL, Verdict.BLOCKED),
        ((Severity.MAJOR,), Severity.MAJOR, Verdict.BLOCKED),
        ((Severity.CRITICAL,), Severity.MINOR, Verdict.BLOCKED),
        ((Severity.INFO, Severity.MINOR), Severity.MAJOR, Verdict.PASS),
    ],
)
def test_the_floor_decides_the_verdict(severities, floor, expected):
    assert gate_for(result(*severities, block_on=floor)).verdict is expected


def test_an_unset_floor_falls_back_to_the_default():
    """A report with no verdict would leave the common case as silent as before."""
    gate = gate_for(result(Severity.MAJOR))
    assert gate.floor is Severity.CRITICAL
    assert gate.verdict is Verdict.PASS
    assert not gate.explicit


def test_a_review_that_did_not_complete_has_no_verdict_to_give():
    """Silence from a review that failed means nothing, so it must not read as a pass."""
    errored = result(errors=["the model timed out"])
    assert gate_for(errored).verdict is Verdict.ERROR

    partial = result(status=ReviewStatus.PARTIAL)
    assert gate_for(partial).verdict is Verdict.ERROR
    assert gate_for(partial).blocked


def test_errors_outrank_findings():
    incomplete = result(Severity.CRITICAL, block_on=Severity.CRITICAL, errors=["boom"])
    assert gate_for(incomplete).verdict is Verdict.ERROR


def test_blocking_findings_are_sorted_most_severe_first():
    mixed = result(Severity.MINOR, Severity.CRITICAL, Severity.MAJOR)
    blocking = blocking_findings(mixed, Severity.MAJOR)
    assert [f.severity for f in blocking] == [Severity.CRITICAL, Severity.MAJOR]


def test_the_counts_line_is_most_severe_first_and_skips_empty_severities():
    gate = gate_for(result(Severity.MINOR, Severity.CRITICAL, Severity.CRITICAL))
    assert gate.counts_line() == "🔴 Critical 2 · 🟡 Minor 1"
    assert gate_for(result()).counts_line() == "none"


def test_the_summary_line_fits_a_forge_status_description():
    for gate in (
        gate_for(result(*[Severity.CRITICAL] * 40, block_on=Severity.MINOR)),
        gate_for(result()),
        gate_for(result(errors=["boom"])),
    ):
        assert len(gate.summary_line()) <= 140


def exit_code(res: ReviewResult, fail_on: Severity | None) -> int:
    with pytest.raises(typer.Exit) as raised:
        finish(res, fail_on)
    return raised.value.exit_code


@pytest.mark.parametrize(
    ("severities", "fail_on", "expected"),
    [
        ((Severity.CRITICAL,), Severity.CRITICAL, EXIT_FINDINGS),
        ((Severity.MINOR,), Severity.CRITICAL, EXIT_OK),
        ((), Severity.INFO, EXIT_OK),
    ],
)
def test_the_exit_code_and_the_rendered_verdict_agree(severities, fail_on, expected):
    """The acceptance criterion: one function, so the two can never diverge."""
    res = result(*severities, block_on=fail_on, block_on_explicit=True)
    assert exit_code(res, fail_on) == expected
    blocked = gate_for(res).verdict is Verdict.BLOCKED
    assert blocked is (expected == EXIT_FINDINGS)


def test_the_configured_default_never_moves_the_exit_code():
    """Otherwise every CI job running roborak without a gate starts failing."""
    res = result(Severity.CRITICAL)
    assert gate_for(res).verdict is Verdict.BLOCKED
    assert exit_code(res, None) == EXIT_OK


def test_an_incomplete_review_still_exits_with_an_error():
    assert exit_code(result(errors=["boom"]), None) == EXIT_ERROR


def test_an_unproven_critical_does_not_reach_the_verdict():
    """The point of the evidence policy, stated where it is finally paid out.

    The demotion happens in the validator; what matters here is that the verdict,
    the block and the exit code all agree afterwards that nothing blocked.
    """
    guess = finding(Severity.CRITICAL)
    guess.confidence = 0.95
    kept = validator.enforce_evidence([guess])

    review = ReviewResult(findings=kept, block_on=Severity.CRITICAL)
    gate = gate_for(review)
    assert kept[0].kind is Kind.VERIFICATION_NEEDED
    assert gate.verdict is Verdict.PASS
    assert gate.blocking == []
    assert "No findings at or above critical" in gate.summary_line()


def checks(*entries: tuple[Enforcement, CheckOutcome], advisory: bool = False) -> ChecksReport:
    return ChecksReport(
        results=[
            CheckResult(
                check=CheckId.TITLE if index == 0 else CheckId.DESCRIPTION,
                level=level,
                outcome=outcome,
                summary="Because.",
                advisory=advisory,
            )
            for index, (level, outcome) in enumerate(entries)
        ]
    )


def test_an_error_check_blocks_with_no_finding_anywhere_near_the_floor():
    """Enforcement is a second, independent reason to block. It is not a severity."""
    gate = gate_for(result(Severity.MINOR, block_on=Severity.CRITICAL, checks=checks(BAD_ERROR)))
    assert gate.verdict is Verdict.BLOCKED
    assert gate.blocking == []
    assert [check.check for check in gate.failed_checks] == [CheckId.TITLE]


def test_a_warning_check_is_reported_and_leaves_the_verdict_alone():
    gate = gate_for(result(block_on=Severity.CRITICAL, checks=checks(BAD_WARNING)))
    assert gate.verdict is Verdict.PASS
    assert gate.failed_checks == []


def test_an_advisory_failure_never_blocks_however_it_is_configured():
    """A model's opinion is not evidence, and evidence is what blocking takes."""
    gate = gate_for(result(block_on=Severity.CRITICAL, checks=checks(BAD_ERROR, advisory=True)))
    assert gate.verdict is Verdict.PASS


@pytest.mark.parametrize(
    "outcome",
    [CheckOutcome.PASSED, CheckOutcome.NOT_APPLICABLE, CheckOutcome.INCONCLUSIVE],
)
def test_only_a_failure_reaches_the_verdict(outcome):
    gate = gate_for(result(block_on=Severity.CRITICAL, checks=checks((Enforcement.ERROR, outcome))))
    assert gate.verdict is Verdict.PASS


def test_a_failed_error_check_does_not_move_the_exit_code():
    """The verdict, the report and the forge status move. The exit code is
    ``--fail-on``'s alone, and widening it would collapse a distinction roborak
    keeps on purpose."""
    blocked = result(block_on=Severity.CRITICAL, checks=checks(BAD_ERROR))
    with pytest.raises(typer.Exit) as exit_without_gate:
        finish(blocked, None)
    assert exit_without_gate.value.exit_code == EXIT_OK

    with pytest.raises(typer.Exit) as exit_with_gate:
        finish(blocked, Severity.CRITICAL)
    assert exit_with_gate.value.exit_code == EXIT_OK


def test_the_summary_line_names_both_reasons_and_stays_short_enough_to_post():
    """Forge status descriptions are capped at 140 characters."""
    both = result(
        Severity.CRITICAL,
        block_on=Severity.CRITICAL,
        checks=checks(BAD_ERROR, (Enforcement.ERROR, CheckOutcome.FAILED)),
    )
    line = gate_for(both).summary_line()
    assert "1 finding at or above critical" in line
    assert "2 pre-merge checks failed" in line
    assert len(line) <= 140


def test_a_check_only_block_does_not_claim_zero_findings():
    line = gate_for(result(block_on=Severity.CRITICAL, checks=checks(BAD_ERROR))).summary_line()
    assert line == "1 pre-merge check failed."
