from evals.run import score


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
