"""Hold back findings a reviewer has already dismissed.

A review bot that repeats a finding someone has explained away teaches its
readers to stop reading it. This drops those repeats, and only those: the match
is on the finding's own fingerprints, which name its file, so a dismissal never
reaches past the shape of what was dismissed. Nothing here is silent -- every
finding held back is listed in the report, and a static finding is kept unless
the project explicitly said otherwise, because a tool ran.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from roborak.core.config import FeedbackConfig
from roborak.core.models import FeedbackReport, Finding, ReviewResult, SuppressedFinding
from roborak.publish.threads import Dismissal
from roborak.state.store import FeedbackEntry


def entries_from(dismissals: Iterable[Dismissal]) -> list[tuple[str, FeedbackEntry]]:
    """One state entry per fingerprint a dismissal names."""
    return [
        (
            fingerprint,
            FeedbackEntry(
                verdict=dismissal.verdict,
                author=dismissal.author,
                file=dismissal.file,
                title=dismissal.title,
                recorded_at=dismissal.recorded_at,
            ),
        )
        for dismissal in dismissals
        for fingerprint in sorted(dismissal.fingerprints)
    ]


def apply_feedback(
    result: ReviewResult,
    entries: Mapping[str, FeedbackEntry],
    config: FeedbackConfig,
) -> None:
    """Remove dismissed findings from ``result`` and record what was removed."""
    if not config.enabled or not entries:
        return

    report = FeedbackReport()
    kept: list[Finding] = []
    for finding in result.findings:
        match = _match(finding, entries)
        if match is None:
            kept.append(finding)
            continue
        fingerprint, entry = match
        if finding.source == "static" and not config.suppress_static:
            report.static_kept += 1
            kept.append(finding)
            continue
        report.suppressed.append(
            SuppressedFinding(
                location=finding.location,
                title=finding.title,
                severity=finding.severity,
                source=finding.source,
                fingerprint=fingerprint,
                verdict=entry.verdict,
                author=entry.author,
            )
        )

    if report.is_empty:
        return
    result.findings = kept
    result.feedback = report


def _match(
    finding: Finding, entries: Mapping[str, FeedbackEntry]
) -> tuple[str, FeedbackEntry] | None:
    for fingerprint in (finding.fingerprint_v2, finding.fingerprint):
        if (entry := entries.get(fingerprint)) is not None:
            return fingerprint, entry
    return None
