"""Run the live-model reviewer quality corpus and emit machine-readable metrics."""

from __future__ import annotations

import argparse
import difflib
import json
import os
from pathlib import Path

import yaml

from evals.judge import JUDGE_FIELDS, SUGGESTION_FIELD, judge_finding
from roborak.analysis.reviewer import Reviewer
from roborak.context.chunker import ChunkStrategy
from roborak.context.diff import parse_diff
from roborak.core.config import Config
from roborak.core.models import ChangeSet, Finding, ReviewResult
from roborak.core.severity import Evidence, Severity
from roborak.core.verdict import blocking_findings
from roborak.llm.client import LLMClient
from roborak.render.markdown import Form, finding_markdown

ROOT = Path(__file__).parent

QUALITY_KINDS = ("clean_control", "supported_defect", "unverified_concern")
QUALITY_CHECKS = (*JUDGE_FIELDS, SUGGESTION_FIELD)


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


def quality_score(rows: list[dict[str, object]]) -> dict[str, float | int | None]:
    """Score one pass over the output-quality corpus, rubric check by rubric check.

    Each check is a pass rate over the verdicts that graded it, so ``suggestion_safe``
    only counts findings that offered a suggestion. A check nothing graded is ``None``,
    never 1.0 -- an unassessed finding is not a good one. A row that records a
    ``judge`` key was an attempt: a dict when the judge answered, ``None`` when it
    could not, and ``judge_completion`` keeps the second from passing quietly.
    """
    attempts = [row for row in rows if "judge" in row]
    graded = [verdict for row in attempts if isinstance(verdict := row["judge"], dict)]
    checks = [bool(passed) for verdict in graded for passed in verdict.values()]
    metrics: dict[str, float | int | None] = {
        "cases": len(rows),
        "judge_attempts": len(attempts),
        "judged": len(graded),
        "judge_completion": len(graded) / len(attempts) if attempts else None,
        "overall": sum(checks) / len(checks) if checks else None,
        "unsupported_claims": sum(int(str(row.get("unsupported_claims") or 0)) for row in rows),
    }
    for check in QUALITY_CHECKS:
        answered = [bool(verdict[check]) for verdict in graded if check in verdict]
        metrics[check] = sum(answered) / len(answered) if answered else None
    return metrics


def quality_by_kind(rows: list[dict[str, object]]) -> dict[str, dict[str, float | int | None]]:
    return {
        kind: quality_score([row for row in rows if row.get("kind") == kind])
        for kind in QUALITY_KINDS
    }


def summarize_quality_runs(
    runs: list[dict[str, float | int | None]],
) -> dict[str, dict[str, float]]:
    """Mean, min and max of every metric across repeated quality runs.

    The model is not deterministic, so one sample says little. Reporting the spread
    lets a reader tell a real shift from one unlucky run; a metric no run produced
    is left out rather than averaged as zero.
    """
    summary: dict[str, dict[str, float]] = {}
    for key in runs[0] if runs else ():
        values = [float(value) for run in runs if (value := run.get(key)) is not None]
        if values:
            summary[key] = {
                "mean": sum(values) / len(values),
                "min": min(values),
                "max": max(values),
            }
    return summary


def quality_delta(
    current: dict[str, dict[str, float]], baseline: dict[str, dict[str, float]]
) -> dict[str, float]:
    """Current mean minus baseline mean, for every metric both runs produced."""
    return {
        key: current[key]["mean"] - baseline[key]["mean"]
        for key in current
        if key in baseline and "mean" in baseline[key]
    }


def gate_failed(metrics: dict[str, float | int], chunking: dict[str, object]) -> bool:
    """Whether the detection and anchoring gates fail the run.

    Output quality is deliberately absent: it is graded by a model about a model, and
    a single unlucky sample must not read as a regression. It is reported, compared
    against a baseline, and left to a maintainer to judge.
    """
    baseline = chunking["baseline"]
    semantic = chunking["semantic"]
    assert isinstance(baseline, dict) and isinstance(semantic, dict)
    return bool(
        metrics["recall"] < 0.80
        or metrics["clean_false_positive_rate"] > 0.10
        or metrics["unproven_blocker_rate"] > 0.10
        or metrics["blocker_recall"] < 0.80
        or metrics["anchor_accuracy"] < 0.95
        or metrics["parse_success"] < 0.99
        or float(semantic["recall"]) < float(baseline["recall"])
        or float(semantic["clean_false_positive_rate"])
        > float(baseline["clean_false_positive_rate"])
    )


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


def _diff_text(case: dict[str, object]) -> str:
    raw_files = case.get("files")
    if isinstance(raw_files, list):
        return "".join(
            synthetic_diff(str(file["path"]), str(file["before"]), str(file["after"]))
            for file in raw_files
            if isinstance(file, dict)
        )
    return synthetic_diff(str(case["path"]), str(case["before"]), str(case["after"]))


def after_text(case: dict[str, object], path: str) -> str | None:
    """The post-change content of ``path`` in a case, if the case touches it."""
    raw_files = case.get("files")
    if isinstance(raw_files, list):
        for file in raw_files:
            if isinstance(file, dict) and str(file["path"]) == path:
                return str(file["after"])
        return None
    return str(case["after"]) if str(case.get("path")) == path else None


def replaced_lines(case: dict[str, object], finding: Finding) -> str | None:
    """The new-file lines a finding's suggestion would replace, or ``None`` without one.

    A suggestion on a file the case never touched gets an empty string rather than
    ``None``: it still has to be graded, and it cannot apply.
    """
    if not finding.suggestion:
        return None
    text = after_text(case, finding.file)
    if text is None:
        return ""
    return "\n".join(text.splitlines()[finding.start_line - 1 : finding.end_line])


def _changeset(case: dict[str, object]) -> ChangeSet:
    return ChangeSet(files=parse_diff(_diff_text(case)), title=str(case["id"]))


def _near(case: dict[str, object], findings: list[Finding]) -> tuple[list[Finding], list[Finding]]:
    """Findings of the expected category and file, and those within 3 lines of the defect."""
    expected = case.get("expected_category")
    expected_file = str(case.get("expected_file") or "")
    line = int(case.get("expected_line") or 0)
    candidates = [
        finding
        for finding in findings
        if finding.category.value == expected
        and (not expected_file or finding.file == expected_file)
    ]
    return candidates, [finding for finding in candidates if abs(finding.start_line - line) <= 3]


def _review(
    case: dict[str, object], config: Config, strategy: ChunkStrategy = "semantic"
) -> ReviewResult:
    return Reviewer(
        config=config,
        repo=ROOT.parent,
        llm=LLMClient(config.llm),
        chunk_strategy=strategy,
    ).review(_changeset(case))


def _evaluate(
    case: dict[str, object],
    config: Config,
    *,
    strategy: ChunkStrategy = "semantic",
) -> dict[str, object]:
    result = _review(case, config, strategy)
    expected = case.get("expected_category")
    line = int(case.get("expected_line") or 0)
    candidates, near = _near(case, result.findings)
    blockers = blocking_findings(result, Severity.MAJOR)
    row: dict[str, object] = {
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
    return row


def _evaluate_quality(
    case: dict[str, object], config: Config, judge: LLMClient
) -> dict[str, object]:
    """Review one quality case and grade the finding a reader would act on.

    What counts as an unsupported claim depends on the case: any finding on a clean
    control, and a blocker on a concern the diff alone cannot prove. A supported
    defect the reviewer missed is not graded -- recall belongs to the detection
    corpus, and this suite only asks whether what *was* said is worth reading.
    """
    result = _review(case, config)
    kind = str(case["kind"])
    blockers = blocking_findings(result, Severity.MAJOR)
    row: dict[str, object] = {
        "id": case["id"],
        "kind": kind,
        "findings": len(result.findings),
        "blockers": len(blockers),
        "errors": result.errors,
        "tokens": result.tokens_used,
    }
    graded: list[Finding] = []
    if kind == "clean_control":
        # The case defines any finding as wrong, so there is nothing to grade.
        row["unsupported_claims"] = len(result.findings)
    elif kind == "supported_defect":
        _, near = _near(case, result.findings)
        row["matched"] = bool(near)
        graded = near[:1]
    else:
        row["unsupported_claims"] = len(blockers)
        graded = result.findings[:1]
    if graded:
        finding = graded[0]
        row["suggestion"] = finding.suggestion is not None
        row["judge"] = judge_finding(
            judge,
            diff=_diff_text(case),
            rendered_finding=finding_markdown(finding, form=Form.PUBLISHED),
            evidence_unverified=finding.evidence is Evidence.UNVERIFIED,
            replaced_lines=replaced_lines(case, finding),
        )
    return row


def _load_baseline(path: Path) -> dict[str, dict[str, float]]:
    """The quality summary of an earlier results file, to compare this run against."""
    loaded = json.loads(path.read_text(encoding="utf-8"))
    summary = loaded.get("output_quality", {}).get("summary") if isinstance(loaded, dict) else None
    if not isinstance(summary, dict):
        raise ValueError(f"{path} has no output_quality summary")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default=os.getenv("ROBORAK_EVAL_MODEL"))
    parser.add_argument("--judge-model", default=os.getenv("ROBORAK_EVAL_JUDGE_MODEL"))
    parser.add_argument("--output", type=Path, default=ROOT / "eval-results.json")
    parser.add_argument(
        "--quality-runs",
        type=int,
        default=3,
        help="passes over the output-quality corpus; the summary reports mean, min, max",
    )
    parser.add_argument(
        "--baseline",
        type=Path,
        help="an earlier results JSON whose output-quality summary this run is compared to",
    )
    args = parser.parse_args()
    if args.quality_runs < 1:
        parser.error("--quality-runs must be at least 1")
    baseline: dict[str, dict[str, float]] | None = None
    if args.baseline:
        # Read before any model call: a bad path should not cost a full run.
        try:
            baseline = _load_baseline(args.baseline)
        except (OSError, ValueError) as error:
            parser.error(f"--baseline: {error}")

    config = Config()
    if args.model:
        config.llm.model = args.model
    config.output.walkthrough = False
    config.static.enabled = False

    # The judge grades prose, so it runs deterministically on its own config: a
    # separate model when one is given, the review model otherwise, always at
    # temperature 0 so the spread across quality runs is the reviewer's, not the
    # judge's.
    judge_config = config.llm.model_copy(deep=True)
    if args.judge_model:
        judge_config.model = args.judge_model
    judge_config.temperature = 0.0
    judge = LLMClient(judge_config)

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

    quality_cases = yaml.safe_load((ROOT / "quality_cases.yaml").read_text(encoding="utf-8"))
    quality_runs = [
        [_evaluate_quality(case, config, judge) for case in quality_cases]
        for _ in range(args.quality_runs)
    ]
    output_quality: dict[str, object] = {
        "summary": summarize_quality_runs([quality_score(run) for run in quality_runs]),
        "runs": [
            {"metrics": quality_score(run), "by_kind": quality_by_kind(run), "cases": run}
            for run in quality_runs
        ],
    }
    if baseline is not None:
        summary = output_quality["summary"]
        assert isinstance(summary, dict)
        output_quality["baseline"] = str(args.baseline)
        output_quality["delta"] = quality_delta(summary, baseline)

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
                "output_quality": output_quality,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    report = {
        "metrics": metrics,
        "chunking_comparison": chunking,
        "output_quality": {
            key: value for key, value in output_quality.items() if key in ("summary", "delta")
        },
    }
    print(json.dumps(report, indent=2))
    return int(gate_failed(metrics, chunking))


if __name__ == "__main__":
    raise SystemExit(main())
