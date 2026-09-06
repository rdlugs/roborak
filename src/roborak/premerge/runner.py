"""The pre-merge check stage.

Follows the shape of ``static.runner`` and ``verify.runner``: config decides what
runs, the runner owns the bounds, and nothing it does can end a review. A check
that raises is recorded as a note and the others still run, because a stage that
takes the whole review down with it is worse than a stage that reports less.

A check set to ``off`` produces no result at all -- not a skipped row. "Nobody
asked" and "we asked and there was nothing to say" are different claims, and only
the second one is worth a line in the report.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from roborak.core.config import PreMergeConfig
from roborak.core.models import (
    ChangeSet,
    CheckId,
    CheckOutcome,
    CheckResult,
    ChecksReport,
    Issue,
    Walkthrough,
)
from roborak.core.severity import Enforcement
from roborak.premerge import docstrings
from roborak.premerge.opinion import Complete, apply_opinion
from roborak.premerge.text import check_description, check_linked_issue, check_title

log = logging.getLogger(__name__)


def run_checks(
    changeset: ChangeSet,
    config: PreMergeConfig,
    *,
    issue: Issue | None = None,
    walkthrough: Walkthrough | None = None,
    complete: Complete | None = None,
) -> ChecksReport:
    """Every configured pre-merge check, deterministic first, opinion second.

    ``complete`` is ``None`` under ``--no-llm`` and whenever no model is
    configured, which is exactly why the gates are deterministic: the checks a
    project gates its merges on must not depend on a provider being reachable.
    """
    report = ChecksReport()

    if config.docstring_coverage.level is not Enforcement.OFF:
        _run(report, lambda: _docstring_coverage(changeset, config), CheckId.DOCSTRING_COVERAGE)
    if config.title.level is not Enforcement.OFF:
        _run(report, lambda: check_title(changeset, config.title.level), CheckId.TITLE)
    if config.description.level is not Enforcement.OFF:
        _run(
            report,
            lambda: check_description(changeset, config.description.level),
            CheckId.DESCRIPTION,
        )
    if config.linked_issue.level is not Enforcement.OFF:
        _run(
            report,
            lambda: check_linked_issue(changeset, issue, config.linked_issue.level),
            CheckId.LINKED_ISSUE,
        )

    if complete is not None and _wants_opinion(config) and report.results:
        note = apply_opinion(report.results, changeset, issue, walkthrough, complete)
        if note:
            report.notes.append(note)
    return report


def _wants_opinion(config: PreMergeConfig) -> bool:
    """Whether a model is worth a call here.

    Only when the project set one of the text checks to ``error``. The opinion is
    advisory by construction -- it can never block on its own -- so at ``warning``
    it would buy a note in the report at the price of an extra call on every
    review, for every user, forever. A project that raised a check to ``error``
    has said this matters enough to spend one.
    """
    return any(
        check.level is Enforcement.ERROR
        for check in (config.title, config.description, config.linked_issue)
    )


def _run(report: ChecksReport, check: Callable[[], CheckResult], name: CheckId) -> None:
    """Run one check, recording a failure to run as a note rather than a verdict."""
    try:
        report.results.append(check())
    except Exception as exc:
        log.debug("pre-merge check %s failed to run", name, exc_info=True)
        report.notes.append(f"The {name.value} check could not run: {exc}")


def _docstring_coverage(changeset: ChangeSet, config: PreMergeConfig) -> CheckResult:
    """Documentation coverage over the symbols the diff touched."""
    level = config.docstring_coverage.level
    threshold = config.docstring_coverage.threshold
    measurement = docstrings.measure(changeset)
    ratio = measurement.ratio

    if ratio is None:
        summary = "No documentable symbols were touched."
        if measurement.unparsed_files:
            summary = "None of the changed files could be parsed for symbols."
        return CheckResult(
            check=CheckId.DOCSTRING_COVERAGE,
            level=level,
            outcome=CheckOutcome.NOT_APPLICABLE,
            summary=summary,
            detail=_unparsed_note(measurement.unparsed_files),
            threshold=threshold,
        )

    counted = f"{measurement.documented} of {measurement.total} touched symbols documented"
    passed = ratio >= threshold
    detail_parts = []
    if not passed:
        listed = "\n".join(
            f"- `{symbol.path}:{symbol.line}` {symbol.name}"
            for symbol in measurement.undocumented[:10]
        )
        detail_parts.append(f"Undocumented:\n{listed}")
        if len(measurement.undocumented) > 10:
            detail_parts.append(f"...and {len(measurement.undocumented) - 10} more.")
    if note := _unparsed_note(measurement.unparsed_files):
        detail_parts.append(note)

    return CheckResult(
        check=CheckId.DOCSTRING_COVERAGE,
        level=level,
        outcome=CheckOutcome.PASSED if passed else CheckOutcome.FAILED,
        summary=f"{counted} ({ratio:.0%}, threshold {threshold:.0%}).",
        detail="\n\n".join(detail_parts),
        measured=ratio,
        threshold=threshold,
    )


def _unparsed_note(paths: list[str]) -> str:
    """Say what was not measured, rather than counting it as undocumented."""
    if not paths:
        return ""
    listed = ", ".join(f"`{path}`" for path in paths[:5])
    more = f" and {len(paths) - 5} more" if len(paths) > 5 else ""
    return f"Not measured, no grammar available: {listed}{more}."
