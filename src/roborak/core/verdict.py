"""The pass/fail verdict, computed once.

``--fail-on`` has always decided whether a change should be blocked, but only ever
said so through an exit code. The verdict now also ends the rendered report, rides
along on the published summary comment, and lands on the forge as a commit status.
Three surfaces stating the same thing is only safe if one function decides it, so
this module is that function and nothing here imports ``render`` or ``publish``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from roborak.core.icons import SEVERITY_LABEL
from roborak.core.models import Finding, ReviewResult, ReviewStatus
from roborak.core.severity import Severity


class Verdict(StrEnum):
    """What the review concluded. One value per exit code roborak can produce."""

    PASS = "pass"
    """Nothing reached the floor. ``EXIT_OK``; ``success`` on the forge."""

    BLOCKED = "blocked"
    """At least one finding reached the floor. ``EXIT_FINDINGS``; ``failure``."""

    ERROR = "error"
    """The review did not complete, so its silence means nothing. ``EXIT_ERROR``."""


def blocking_findings(result: ReviewResult, floor: Severity) -> list[Finding]:
    """The findings that reach ``floor``.

    The single predicate behind the exit code, the rendered block and the forge
    status. Anything that wants to know "is this blocked?" asks here.
    """
    return sorted(
        (finding for finding in result.all_findings() if finding.severity.at_least(floor)),
        key=lambda finding: (
            -finding.severity.rank,
            -finding.confidence,
            finding.file,
            finding.start_line,
        ),
    )


@dataclass(frozen=True)
class Gate:
    """A verdict and the evidence for it, ready for any surface to state."""

    verdict: Verdict
    floor: Severity
    explicit: bool
    """The floor came from ``--fail-on`` rather than the ``review.block_on`` default.
    Only an explicit floor is wired to the exit code, and the rendered block says so."""

    blocking: list[Finding]
    counts: dict[Severity, int]

    @property
    def blocked(self) -> bool:
        return self.verdict is not Verdict.PASS

    def summary_line(self) -> str:
        """One sentence, short enough for a forge status description (140 chars)."""
        if self.verdict is Verdict.ERROR:
            return "Review did not complete; no verdict."
        if self.verdict is Verdict.BLOCKED:
            n = len(self.blocking)
            plural = "" if n == 1 else "s"
            return f"{n} finding{plural} at or above {self.floor}."
        return f"No findings at or above {self.floor}."

    def counts_line(self) -> str:
        """The severities that drove the verdict, most severe first.

        Iterating ``Severity`` gives that order for free -- the enum is declared
        critical-first, which is what the severity table relies on too.
        """
        parts = [f"{SEVERITY_LABEL[s]} {n}" for s, n in self.counts.items() if n]
        return " · ".join(parts) if parts else "none"


DEFAULT_FLOOR = Severity.CRITICAL
"""The floor a verdict falls back to when ``--fail-on`` said nothing.

A report that showed no verdict unless the reader happened to pass a flag would
leave the common case -- a local run, a review posted from CI without a gate --
exactly as silent as before. See ``ReviewConfig.block_on``.
"""


def verdict_requested(result: ReviewResult) -> bool:
    """Whether this result came from something that actually looked for findings.

    ``roborak describe`` renders a walkthrough through the same document as a
    review, but it never reviews anything -- so stating "pass" there would assert
    a verdict nobody reached. Only ``review`` and ``improve`` record a floor,
    which makes ``block_on`` the honest test for "was anything judged here".
    """
    return result.block_on is not None


def gate_for(result: ReviewResult) -> Gate:
    """The verdict as rendered and published, from the floor the CLI recorded."""
    floor = result.block_on or DEFAULT_FLOOR
    blocking = blocking_findings(result, floor)
    if result.errors or result.status is not ReviewStatus.COMPLETE:
        verdict = Verdict.ERROR
    elif blocking:
        verdict = Verdict.BLOCKED
    else:
        verdict = Verdict.PASS
    return Gate(
        verdict=verdict,
        floor=floor,
        explicit=result.block_on_explicit,
        blocking=blocking,
        counts=result.counts_by_severity,
    )
