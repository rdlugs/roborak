"""Vocabulary for findings.

A finding is described along five axes: ``Severity`` and ``Category`` say how
much it matters and what domain it belongs to, ``Kind`` and ``Effort`` say what
sort of comment it is and what fixing it will cost, and ``Evidence`` says what
makes it true. Every value is lowercase so it round-trips through YAML, JSON and
the LLM prompt without normalisation.

How these axes are *shown* -- the glyph beside a severity, the label a category
prints as -- lives in ``core.icons``, so that a change to the look of a report
cannot reach in here and change what a finding means.
"""

from __future__ import annotations

from enum import StrEnum


class Severity(StrEnum):
    CRITICAL = "critical"
    MAJOR = "major"
    MINOR = "minor"
    INFO = "info"

    @property
    def rank(self) -> int:
        """Higher is more severe. Used for sorting and for the severity floor."""
        return _SEVERITY_RANK[self]

    def at_least(self, floor: Severity) -> bool:
        return self.rank >= floor.rank


_SEVERITY_RANK: dict[Severity, int] = {
    Severity.INFO: 0,
    Severity.MINOR: 1,
    Severity.MAJOR: 2,
    Severity.CRITICAL: 3,
}


class Category(StrEnum):
    SECURITY = "security"
    BUG = "bug"
    PERFORMANCE = "performance"
    LOGIC = "logic"
    MAINTAINABILITY = "maintainability"
    TESTING = "testing"
    STYLE = "style"
    DOCS = "docs"


class Kind(StrEnum):
    """What sort of comment this is: a problem, a suggestion, or an aside."""

    POTENTIAL_ISSUE = "potential_issue"
    REFACTOR_SUGGESTION = "refactor_suggestion"
    NITPICK = "nitpick"
    VERIFICATION_NEEDED = "verification_needed"
    REQUIREMENT_GAP = "requirement_gap"
    """Something the issue asked for that the change does not appear to do. The one
    kind that is not anchored to a changed line, because an omission has none."""


class Evidence(StrEnum):
    """What makes a finding true, as opposed to how sure the model feels.

    ``confidence`` is the model grading its own homework. This says what the
    finding can actually point at, which is the only thing that separates a traced
    failure from a plausible guess wearing a high number.
    """

    EXECUTION_PATH = "execution_path"
    """A concrete trigger and the path from it to the failure."""

    REPRODUCTION = "reproduction"
    """An input and the wrong result it produces."""

    CONTRACT = "contract"
    """A documented or declared contract the change violates."""

    STATIC_TOOL = "static_tool"
    """A tool ran and said so. Only static findings claim this."""

    UNVERIFIED = "unverified"
    """Reasoning alone. Honest, and not grounds to block a merge."""

    @property
    def proven(self) -> bool:
        return self is not Evidence.UNVERIFIED


class Effort(StrEnum):
    """What the fix is likely to cost."""

    QUICK_WIN = "quick_win"
    MODERATE = "moderate"
    HEAVY_LIFT = "heavy_lift"


SEVERITY_STYLE: dict[Severity, str] = {
    Severity.CRITICAL: "bold red",
    Severity.MAJOR: "bold yellow",
    Severity.MINOR: "cyan",
    Severity.INFO: "dim",
}

EVIDENCE_LABEL: dict[Evidence, str] = {
    Evidence.EXECUTION_PATH: "Execution path",
    Evidence.REPRODUCTION: "Reproduction",
    Evidence.CONTRACT: "Contract",
    Evidence.STATIC_TOOL: "Static tool",
    Evidence.UNVERIFIED: "Unverified",
}

KIND_LABEL: dict[Kind, str] = {
    Kind.POTENTIAL_ISSUE: "Potential issue",
    Kind.REFACTOR_SUGGESTION: "Refactor suggestion",
    Kind.NITPICK: "Nitpick",
    Kind.VERIFICATION_NEEDED: "Verification needed",
    Kind.REQUIREMENT_GAP: "Requirement gap",
}
