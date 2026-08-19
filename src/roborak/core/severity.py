"""Vocabulary for findings.

The enums here deliberately merge the three tools roborak models itself on:
``Severity``/``Category`` follow Kodus, ``Kind``/``Effort`` follow CodeRabbit's
comment taxonomy. Every value is lowercase so it round-trips through YAML, JSON
and the LLM prompt without normalisation.
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
    """What sort of comment this is, in CodeRabbit's sense."""

    POTENTIAL_ISSUE = "potential_issue"
    REFACTOR_SUGGESTION = "refactor_suggestion"
    NITPICK = "nitpick"
    VERIFICATION_NEEDED = "verification_needed"


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

KIND_LABEL: dict[Kind, str] = {
    Kind.POTENTIAL_ISSUE: "Potential issue",
    Kind.REFACTOR_SUGGESTION: "Refactor suggestion",
    Kind.NITPICK: "Nitpick",
    Kind.VERIFICATION_NEEDED: "Verification needed",
}
