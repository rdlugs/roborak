"""Run the live-model reviewer quality corpus and emit machine-readable metrics."""

from __future__ import annotations

import argparse
import difflib
import json
import os
from pathlib import Path

import yaml

from roborak.analysis.reviewer import Reviewer
from roborak.context.chunker import ChunkStrategy
from roborak.context.diff import parse_diff
from roborak.core.config import Config
from roborak.core.models import ChangeSet
from roborak.core.severity import Severity
from roborak.core.verdict import blocking_findings
from roborak.llm.client import LLMClient

ROOT = Path(__file__).parent


def synthetic_diff(path: str, before: str, after: str) -> str:
    body = "\n".join(
        difflib.unified_diff(
            before.splitlines(),
            after.splitlines(),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            lineterm="",
        )
    )
    return f"diff --git a/{path} b/{path}\n{body}\n"


def score(rows: list[dict[str, object]]) -> dict[str, float | int]:
    defects = [row for row in rows if row["expected_category"]]
    matched = [row for row in defects if row["matched"]]

    # The evidence policy is a trade, so both halves are measured together: a run
    # that stops blocking on guesses by also refusing to block on real defects has
    # not improved anything.
    controls = [row for row in rows if row.get("expect_blocker") is False]
    provable = [row for row in rows if row.get("expect_blocker") is True]

    # The controls are *meant* to draw a nonblocking finding, so they are not
    # false positives; only the cases expected to stay silent are.
    clean = [
        row
        for row in rows
        if not row["expected_category"] and row.get("expect_blocker") is not False
    ]

    return {
        "cases": len(rows),
        "recall": len(matched) / len(defects) if defects else 1.0,
        "clean_false_positive_rate": (
            sum(bool(row["findings"]) for row in clean) / len(clean) if clean else 0.0
        ),
        "unproven_blocker_rate": (
            sum(bool(row["blockers"]) for row in controls) / len(controls) if controls else 0.0
        ),
        "blocker_recall": (
            sum(bool(row["matched_blocker"]) for row in provable) / len(provable)
            if provable
            else 1.0
        ),
        "anchor_accuracy": (
            sum(bool(row["exact_anchor"]) for row in matched) / len(matched) if matched else 0.0
        ),
        "parse_success": sum(not row["errors"] for row in rows) / len(rows) if rows else 1.0,
        "tokens": sum(int(row["tokens"]) for row in rows),
    }


def compare_chunking(
    baseline_rows: list[dict[str, object]], semantic_rows: list[dict[str, object]]
) -> dict[str, object]:
    """Compare the new planner with the retained directory/language baseline."""
    baseline = score(baseline_rows)
    semantic = score(semantic_rows)
    return {
        "baseline": baseline,
        "semantic": semantic,
        "recall_delta": float(semantic["recall"]) - float(baseline["recall"]),
        "clean_false_positive_rate_delta": float(semantic["clean_false_positive_rate"])
        - float(baseline["clean_false_positive_rate"]),
    }


def _changeset(case: dict[str, object]) -> ChangeSet:
    raw_files = case.get("files")
    if isinstance(raw_files, list):
        diff = "".join(
            synthetic_diff(str(file["path"]), str(file["before"]), str(file["after"]))
            for file in raw_files
            if isinstance(file, dict)
        )
    else:
        diff = synthetic_diff(str(case["path"]), str(case["before"]), str(case["after"]))
    return ChangeSet(files=parse_diff(diff), title=str(case["id"]))


def _evaluate(
    case: dict[str, object], config: Config, *, strategy: ChunkStrategy = "semantic"
) -> dict[str, object]:
    result = Reviewer(
        config=config,
        repo=ROOT.parent,
        llm=LLMClient(config.llm),
        chunk_strategy=strategy,
    ).review(_changeset(case))
    expected = case.get("expected_category")
    expected_file = str(case.get("expected_file") or "")
    line = int(case.get("expected_line") or 0)
    candidates = [
        finding
        for finding in result.findings
        if finding.category.value == expected
        and (not expected_file or finding.file == expected_file)
    ]
    blockers = blocking_findings(result, Severity.MAJOR)
    near = [finding for finding in candidates if abs(finding.start_line - line) <= 3]
    return {
        "id": case["id"],
        "expected_category": expected,
        "expect_blocker": case.get("expect_blocker"),
        "findings": len(result.findings),
        "blockers": len(blockers),
        "matched": bool(near),
        "matched_blocker": any(any(blocker is finding for blocker in blockers) for finding in near),
        "exact_anchor": any(finding.start_line == line for finding in candidates),
        "errors": result.errors,
        "tokens": result.tokens_used,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default=os.getenv("ROBORAK_EVAL_MODEL"))
    parser.add_argument("--output", type=Path, default=ROOT / "eval-results.json")
    args = parser.parse_args()

    config = Config()
    if args.model:
        config.llm.model = args.model
    config.output.walkthrough = False
    config.static.enabled = False
    cases = yaml.safe_load((ROOT / "cases.yaml").read_text(encoding="utf-8"))
    rows: list[dict[str, object]] = []

    for case in cases:
        rows.append(_evaluate(case, config))

    metrics = score(rows)
    chunking_cases = yaml.safe_load((ROOT / "chunking_cases.yaml").read_text(encoding="utf-8"))
    baseline_rows: list[dict[str, object]] = []
    semantic_rows: list[dict[str, object]] = []
    for case in chunking_cases:
        case_config = config.model_copy(deep=True)
        case_config.llm.context_budget = int(case.get("context_budget") or 80)
        baseline_rows.append(_evaluate(case, case_config, strategy="directory"))
        semantic_rows.append(_evaluate(case, case_config, strategy="semantic"))
    chunking = compare_chunking(baseline_rows, semantic_rows)
    args.output.write_text(
        json.dumps(
            {
                "model": config.model,
                "metrics": metrics,
                "cases": rows,
                "chunking_comparison": chunking,
                "chunking_cases": {
                    "baseline": baseline_rows,
                    "semantic": semantic_rows,
                },
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(json.dumps({"metrics": metrics, "chunking_comparison": chunking}, indent=2))
    baseline_metrics = chunking["baseline"]
    semantic_metrics = chunking["semantic"]
    assert isinstance(baseline_metrics, dict) and isinstance(semantic_metrics, dict)
    return int(
        metrics["recall"] < 0.80
        or metrics["clean_false_positive_rate"] > 0.10
        or metrics["unproven_blocker_rate"] > 0.10
        or metrics["blocker_recall"] < 0.80
        or metrics["anchor_accuracy"] < 0.95
        or metrics["parse_success"] < 0.99
        or float(semantic_metrics["recall"]) < float(baseline_metrics["recall"])
        or float(semantic_metrics["clean_false_positive_rate"])
        > float(baseline_metrics["clean_false_positive_rate"])
    )


if __name__ == "__main__":
    raise SystemExit(main())
