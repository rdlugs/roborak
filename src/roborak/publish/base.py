"""Shared publishing concerns.

The one thing every publisher does identically: turn a ``Finding`` into markdown
carrying a committable suggestion block, in the syntax the forge understands.
"""

from __future__ import annotations

from typing import Protocol

from roborak.core.buckets import SUMMARY_BUCKETS, Bucket, group
from roborak.core.models import Finding, ReviewResult
from roborak.core.severity import Kind
from roborak.render import markdown


class Publisher(Protocol):
    def publish(self, result: ReviewResult) -> PublishReport: ...


class PublishReport:
    """What actually happened, so the CLI can report it honestly."""

    def __init__(self) -> None:
        self.posted: list[Finding] = []
        self.skipped_duplicate: list[Finding] = []
        self.failed: list[tuple[Finding, str]] = []
        self.summarised: list[Finding] = []
        """Findings the summary carries instead of the diff: nitpicks, anything
        that could not be anchored, and requirement gaps, which have no honest
        line to point at in the first place."""

        self.summary_posted = False

    @property
    def total_attempted(self) -> int:
        return (
            len(self.posted) + len(self.skipped_duplicate) + len(self.failed) + len(self.summarised)
        )


def finding_markdown(finding: Finding, *, suggestion_syntax: str = "suggestion") -> str:
    """One inline review comment. Rendered by the same code as the report."""
    return markdown.finding_markdown(finding, suggestion_syntax=suggestion_syntax)


def requirement_gaps(result: ReviewResult) -> list[Finding]:
    """Findings that report unmet requirements rather than defects in a line."""
    return [f for f in result.sorted_findings() if f.kind is Kind.REQUIREMENT_GAP]


def inline_findings(result: ReviewResult) -> list[Finding]:
    """The findings that earn an inline thread on the diff.

    Everything else -- nitpicks, unanchorable findings, requirement gaps -- is
    carried by the summary instead. See ``roborak.core.buckets`` for why.
    """
    return group(result).get(Bucket.ACTIONABLE, [])


def summarised_findings(result: ReviewResult) -> list[Finding]:
    """What the summary comment carries because no inline thread could hold it."""
    grouped = group(result)
    return [f for bucket in SUMMARY_BUCKETS for f in grouped.get(bucket, [])]


def summary_markdown(result: ReviewResult) -> str:
    """The comment body: the whole report, exactly as it was printed.

    It repeats the findings that also went out as inline threads, and that is the
    trade being made deliberately. A comment that omitted them would be a fourth
    thing nobody had read before it was published, and the point of this shape is
    that what you saw on screen is what lands on the merge request.
    """
    return markdown.render(result)
