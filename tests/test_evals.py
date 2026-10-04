from pathlib import Path

import pytest
import yaml

from evals.judge import build_judge_prompt, judge_system, parse_judge_reply
from evals.run import (
    QUALITY_KINDS,
    compare_chunking,
    gate_failed,
    quality_by_kind,
    quality_delta,
    quality_score,
    replaced_lines,
    score,
    summarize_quality_runs,
)
from roborak.core.models import Finding
from roborak.core.severity import Category, Severity

EVALS = Path(__file__).parent.parent / "evals"


def test_eval_metrics_are_computed_from_case_outcomes():
    rows = [
        {
            "expected_category": "bug",
            "matched": True,
            "exact_anchor": True,
            "findings": 1,
            "blockers": 1,
            "errors": [],
            "tokens": 10,
        },
        {
            "expected_category": None,
            "matched": False,
            "exact_anchor": False,
            "findings": 0,
            "blockers": 0,
            "errors": [],
            "tokens": 5,
        },
    ]
    metrics = score(rows)
    assert metrics["recall"] == 1.0
    assert metrics["clean_false_positive_rate"] == 0.0
    assert metrics["anchor_accuracy"] == 1.0
    assert metrics["parse_success"] == 1.0
    assert metrics["tokens"] == 15


def _row(*, expect_blocker: bool, blockers: int) -> dict[str, object]:
    return {
        "expected_category": "bug" if expect_blocker else None,
        "expect_blocker": expect_blocker,
        "matched": expect_blocker,
        "matched_blocker": expect_blocker and bool(blockers),
        "exact_anchor": expect_blocker,
        "findings": blockers,
        "blockers": blockers,
        "errors": [],
        "tokens": 1,
    }


def test_the_evidence_metrics_measure_both_halves_of_the_trade():
    """Blocking on nothing scores perfectly on one metric and fails the other."""
    metrics = score(
        [
            _row(expect_blocker=False, blockers=1),
            _row(expect_blocker=False, blockers=0),
            _row(expect_blocker=True, blockers=1),
            _row(expect_blocker=True, blockers=0),
        ]
    )
    assert metrics["unproven_blocker_rate"] == 0.5
    assert metrics["blocker_recall"] == 0.5


def test_blocker_recall_needs_the_blocker_to_be_the_expected_defect():
    """An unrelated major finding cannot stand in for the defect the case tests."""
    row = _row(expect_blocker=True, blockers=1)
    row["matched_blocker"] = False
    assert score([row])["blocker_recall"] == 0.0


def test_nonblocking_controls_are_not_counted_as_clean_false_positives():
    """The controls are meant to draw a finding; only silence-expected cases aren't."""
    metrics = score([_row(expect_blocker=False, blockers=0) | {"findings": 1}])
    assert metrics["clean_false_positive_rate"] == 0.0


def test_rows_without_a_blocker_label_are_left_out_of_both_metrics():
    """The 30 original cases predate the policy and must not skew it."""
    metrics = score(
        [
            {
                "expected_category": "bug",
                "matched": True,
                "exact_anchor": True,
                "findings": 1,
                "blockers": 1,
                "errors": [],
                "tokens": 1,
            }
        ]
    )
    assert metrics["unproven_blocker_rate"] == 0.0
    assert metrics["blocker_recall"] == 1.0


def _quality(kind: str, verdict: dict[str, bool] | None = None, **row: object) -> dict:
    graded = {"judge": verdict} if verdict is not None or row.pop("failed", False) else {}
    return {"kind": kind, "findings": 1, "blockers": 0, "errors": [], "tokens": 1} | graded | row


_PASS = {"states_trigger": True, "states_consequence": True, "states_fix": True, "faithful": True}


def test_quality_is_scored_check_by_check_over_the_verdicts():
    """Each rubric check gets its own rate, and ``overall`` averages every graded check."""
    metrics = quality_score(
        [
            _quality("supported_defect", _PASS),
            _quality("supported_defect", _PASS | {"faithful": False}),
        ]
    )
    assert metrics["judged"] == 2
    assert metrics["judge_attempts"] == 2
    assert metrics["judge_completion"] == 1.0
    assert metrics["states_trigger"] == 1.0
    assert metrics["faithful"] == 0.5
    assert metrics["overall"] == 0.875


def test_suggestion_safety_is_rated_only_over_findings_that_offered_one():
    """A finding without a suggestion is neither a safe nor an unsafe one."""
    metrics = quality_score(
        [
            _quality("supported_defect", _PASS | {"suggestion_safe": False}),
            _quality("supported_defect", _PASS),
        ]
    )
    assert metrics["suggestion_safe"] == 0.0
    assert quality_score([_quality("supported_defect", _PASS)])["suggestion_safe"] is None


def test_unsupported_claims_count_clean_control_findings_and_unproven_blockers():
    metrics = quality_score(
        [
            _quality("clean_control", findings=2, unsupported_claims=2),
            _quality("unverified_concern", _PASS, blockers=1, unsupported_claims=1),
            _quality("unverified_concern", findings=0, unsupported_claims=0),
        ]
    )
    assert metrics["unsupported_claims"] == 3
    by_kind = quality_by_kind(
        [
            _quality("clean_control", unsupported_claims=1),
            _quality("unverified_concern", _PASS),
        ]
    )
    assert by_kind["clean_control"]["unsupported_claims"] == 1
    assert by_kind["unverified_concern"]["judged"] == 1
    assert by_kind["supported_defect"]["cases"] == 0


def test_a_failed_judge_is_an_attempt_that_did_not_complete():
    """``judge: None`` means the judge was asked and could not answer -- not a pass."""
    metrics = quality_score(
        [_quality("supported_defect", _PASS), _quality("supported_defect", failed=True)]
    )
    assert metrics["judge_attempts"] == 2
    assert metrics["judged"] == 1
    assert metrics["judge_completion"] == 0.5


def test_nothing_graded_is_reported_as_unknown_never_as_perfect():
    metrics = quality_score([_quality("clean_control", findings=0, unsupported_claims=0)])
    assert metrics["judge_attempts"] == 0
    assert metrics["judge_completion"] is None
    assert metrics["overall"] is None
    assert metrics["faithful"] is None


def test_repeated_runs_report_spread_and_skip_metrics_no_run_produced():
    summary = summarize_quality_runs(
        [{"overall": 0.5, "faithful": None}, {"overall": 1.0, "faithful": None}]
    )
    assert summary["overall"] == {"mean": 0.75, "min": 0.5, "max": 1.0}
    assert "faithful" not in summary


def test_quality_delta_compares_means_present_in_both_runs():
    current = {"overall": {"mean": 0.9}, "faithful": {"mean": 0.5}}
    baseline = {"overall": {"mean": 0.8}}
    delta = quality_delta(current, baseline)
    assert delta == {"overall": pytest.approx(0.1)}


def _gate_inputs() -> tuple[dict[str, float | int], dict[str, object]]:
    metrics: dict[str, float | int] = {
        "recall": 1.0,
        "clean_false_positive_rate": 0.0,
        "unproven_blocker_rate": 0.0,
        "blocker_recall": 1.0,
        "anchor_accuracy": 1.0,
        "parse_success": 1.0,
    }
    side = {"recall": 1.0, "clean_false_positive_rate": 0.0}
    return metrics, {"baseline": side, "semantic": dict(side)}


def test_the_gate_reads_detection_metrics_and_never_output_quality():
    """Quality is report-only: a perfect detection run passes whatever the judge said."""
    metrics, chunking = _gate_inputs()
    assert not gate_failed(metrics | {"finding_quality": 0.0, "overall": 0.0}, chunking)
    assert gate_failed(metrics | {"recall": 0.5}, chunking)


def test_replaced_lines_are_the_new_file_lines_under_a_suggestion():
    case = {"path": "a.py", "before": "", "after": "one\ntwo\nthree"}
    finding = Finding(
        file="a.py",
        start_line=2,
        end_line=3,
        severity=Severity.MINOR,
        category=Category.BUG,
        title="t",
        body="b",
        suggestion="TWO",
    )
    assert replaced_lines(case, finding) == "two\nthree"
    assert replaced_lines(case, finding.model_copy(update={"suggestion": None})) is None
    # A suggestion on a file the case never touched cannot apply, but is still graded.
    assert replaced_lines(case, finding.model_copy(update={"file": "b.py"})) == ""


def test_the_quality_corpus_covers_every_kind_and_owns_every_judged_case():
    quality = yaml.safe_load((EVALS / "quality_cases.yaml").read_text(encoding="utf-8"))
    kinds = {case["kind"] for case in quality}
    assert kinds == set(QUALITY_KINDS)
    assert len({case["id"] for case in quality}) == len(quality)
    for case in quality:
        if case["kind"] == "supported_defect":
            assert case["expected_category"] and case["expected_line"]
    detection = yaml.safe_load((EVALS / "cases.yaml").read_text(encoding="utf-8"))
    assert not any("judge" in case for case in detection)


def test_chunking_comparison_reports_recall_and_false_positive_deltas():
    defect = _row(expect_blocker=True, blockers=1)
    missed = defect | {"matched": False, "matched_blocker": False, "blockers": 0}
    clean = {
        "expected_category": None,
        "matched": False,
        "exact_anchor": False,
        "findings": 0,
        "blockers": 0,
        "errors": [],
        "tokens": 1,
    }
    noisy = clean | {"findings": 1}

    comparison = compare_chunking([missed, noisy], [defect, clean])

    assert comparison["recall_delta"] == 1.0
    assert comparison["clean_false_positive_rate_delta"] == -1.0


def test_judge_reply_parses_a_full_boolean_verdict():
    verdict = parse_judge_reply(
        "states_trigger: true\nstates_consequence: true\nstates_fix: false\nfaithful: true\n"
    )
    assert verdict == {
        "states_trigger": True,
        "states_consequence": True,
        "states_fix": False,
        "faithful": True,
    }


def test_judge_reply_is_untrusted_when_a_field_is_missing_or_unparseable():
    """A half-answered or malformed reply graded nothing -- it must not pass by default."""
    missing = parse_judge_reply("states_trigger: true\nstates_consequence: true\n")
    assert missing is None
    non_boolean = parse_judge_reply(
        "states_trigger: maybe\nstates_consequence: true\nstates_fix: true\nfaithful: true\n"
    )
    assert non_boolean is None
    assert parse_judge_reply("not: [valid") is None
    assert parse_judge_reply("just a sentence") is None


def test_suggestion_safety_is_required_only_when_there_is_a_suggestion():
    reply = "states_trigger: true\nstates_consequence: true\nstates_fix: true\nfaithful: true\n"
    assert parse_judge_reply(reply, has_suggestion=True) is None
    assert parse_judge_reply(reply + "suggestion_safe: false\n", has_suggestion=True) == {
        "states_trigger": True,
        "states_consequence": True,
        "states_fix": True,
        "faithful": True,
        "suggestion_safe": False,
    }
    # Without a suggestion the field is not graded, even if the judge volunteers it.
    assert "suggestion_safe" not in (parse_judge_reply(reply + "suggestion_safe: true\n") or {})


def test_the_judge_is_shown_the_lines_a_suggestion_replaces():
    prompt = build_judge_prompt(
        diff="d", rendered_finding="f", evidence_unverified=False, replaced_lines="x = 1"
    )
    assert "# Lines the suggestion replaces\n\nx = 1" in prompt
    assert "Lines the suggestion" not in build_judge_prompt(
        diff="d", rendered_finding="f", evidence_unverified=True
    )
    assert "suggestion_safe" in judge_system(has_suggestion=True)
    assert "suggestion_safe" not in judge_system(has_suggestion=False)
