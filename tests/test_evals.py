from evals.run import score


def test_eval_metrics_are_computed_from_case_outcomes():
    rows = [
        {
            "expected_category": "bug",
            "matched": True,
            "exact_anchor": True,
            "findings": 1,
            "errors": [],
            "tokens": 10,
        },
        {
            "expected_category": None,
            "matched": False,
            "exact_anchor": False,
            "findings": 0,
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
