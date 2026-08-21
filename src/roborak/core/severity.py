"""Vocabulary for findings.

A finding is described along four axes: ``Severity`` and ``Category`` say how
much it matters and what domain it belongs to, ``Kind`` and ``Effort`` say what
sort of comment it is and what fixing it will cost. Every value is lowercase so
it round-trips through YAML, JSON and the LLM prompt without normalisation.
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

# The badge vocabulary every renderer draws from: a finding
# announces its category, how much it matters, and what fixing it will cost, in
# that order. Kept here beside the enums so the four surfaces cannot drift.
CATEGORY_LABEL: dict[Category, str] = {
    Category.SECURITY: "🔒 Security",
    Category.BUG: "🎯 Functional Correctness",
    Category.PERFORMANCE: "🚀 Performance",
    Category.LOGIC: "🧠 Logic",
    Category.MAINTAINABILITY: "📐 Maintainability & Code Quality",
    Category.TESTING: "🧪 Testing",
    Category.STYLE: "🎨 Style",
    Category.DOCS: "📝 Documentation",
}

SEVERITY_LABEL: dict[Severity, str] = {
    Severity.CRITICAL: "🔴 Critical",
    Severity.MAJOR: "🟠 Major",
    Severity.MINOR: "🟡 Minor",
    Severity.INFO: "🔵 Trivial",
}

EFFORT_LABEL: dict[Effort, str] = {
    Effort.QUICK_WIN: "⚡ Quick win",
    Effort.MODERATE: "🔨 Moderate",
    Effort.HEAVY_LIFT: "🏗️ Heavy lift",
}

KIND_LABEL: dict[Kind, str] = {
    Kind.POTENTIAL_ISSUE: "Potential issue",
    Kind.REFACTOR_SUGGESTION: "Refactor suggestion",
    Kind.NITPICK: "Nitpick",
    Kind.VERIFICATION_NEEDED: "Verification needed",
    Kind.REQUIREMENT_GAP: "Requirement gap",
}
