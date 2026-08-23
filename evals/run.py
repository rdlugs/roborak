"""Run the live-model reviewer quality corpus and emit machine-readable metrics."""

from __future__ import annotations

import argparse
import difflib
import json
import os
from pathlib import Path

import yaml

from roborak.analysis.reviewer import Reviewer
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
    clean = [row for row in rows if not row["expected_category"]]
    matched = [row for row in defects if row["matched"]]

    # The evidence policy is a trade, so both halves are measured together: a run
    # that stops blocking on guesses by also refusing to block on real defects has
    # not improved anything.
    controls = [row for row in rows if row.get("expect_blocker") is False]
    provable = [row for row in rows if row.get("expect_blocker") is True]

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
            sum(bool(row["blockers"]) for row in provable) / len(provable) if provable else 1.0
        ),
        "anchor_accuracy": (
            sum(bool(row["exact_anchor"]) for row in matched) / len(matched) if matched else 0.0
        ),
        "parse_success": sum(not row["errors"] for row in rows) / len(rows) if rows else 1.0,
        "tokens": sum(int(row["tokens"]) for row in rows),
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
        diff = synthetic_diff(case["path"], case["before"], case["after"])
        result = Reviewer(config=config, repo=ROOT.parent, llm=LLMClient(config.llm)).review(
            ChangeSet(files=parse_diff(diff), title=case["id"])
        )
        expected = case.get("expected_category")
        line = int(case.get("expected_line") or 0)
        candidates = [finding for finding in result.findings if finding.category.value == expected]
        # The same predicate the verdict blocks on, so the metric cannot drift from
        # what actually fails CI: severity alone decides, whatever the kind.
        blockers = blocking_findings(result, Severity.MAJOR)
        rows.append(
            {
                "id": case["id"],
                "expected_category": expected,
                "expect_blocker": case.get("expect_blocker"),
                "findings": len(result.findings),
                "blockers": len(blockers),
                "matched": any(abs(finding.start_line - line) <= 3 for finding in candidates),
                "exact_anchor": any(finding.start_line == line for finding in candidates),
                "errors": result.errors,
                "tokens": result.tokens_used,
            }
        )

    metrics = score(rows)
    args.output.write_text(
        json.dumps({"model": config.model, "metrics": metrics, "cases": rows}, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(metrics, indent=2))
    return int(
        metrics["recall"] < 0.80
        or metrics["clean_false_positive_rate"] > 0.10
        or metrics["unproven_blocker_rate"] > 0.10
        or metrics["blocker_recall"] < 0.80
        or metrics["anchor_accuracy"] < 0.95
        or metrics["parse_success"] < 0.99
    )


if __name__ == "__main__":
    raise SystemExit(main())
