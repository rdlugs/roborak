"""Map each tool's own vocabulary onto roborak's.

Static tools disagree about what "error" means -- ruff calls a missing import an
error and an unused variable an error too. Mapping by *rule family* rather than by
the tool's severity field is what keeps the merged report coherent.
"""

from __future__ import annotations

from roborak.core.severity import Category, Effort, Kind, Severity

# Ruff rule prefixes, longest-first so `PERF` wins over `P`.
_RUFF_CATEGORIES: list[tuple[str, Category, Severity]] = [
    ("S", Category.SECURITY, Severity.MAJOR),  # flake8-bandit
    ("PERF", Category.PERFORMANCE, Severity.MINOR),
    ("ASYNC", Category.BUG, Severity.MAJOR),
    ("B", Category.BUG, Severity.MAJOR),  # flake8-bugbear
    ("F82", Category.BUG, Severity.CRITICAL),  # undefined name
    ("F", Category.BUG, Severity.MINOR),
    ("E9", Category.BUG, Severity.CRITICAL),  # syntax error
    ("RUF", Category.MAINTAINABILITY, Severity.MINOR),
    ("SIM", Category.MAINTAINABILITY, Severity.MINOR),
    ("C4", Category.PERFORMANCE, Severity.INFO),
    ("N", Category.STYLE, Severity.INFO),
    ("D", Category.DOCS, Severity.INFO),
    ("ANN", Category.MAINTAINABILITY, Severity.INFO),
    ("PT", Category.TESTING, Severity.MINOR),
    ("E", Category.STYLE, Severity.INFO),
    ("W", Category.STYLE, Severity.INFO),
    ("I", Category.STYLE, Severity.INFO),
    ("UP", Category.MAINTAINABILITY, Severity.INFO),
]


def classify_ruff(code: str) -> tuple[Category, Severity]:
    """Longest matching prefix wins, so specific families beat their parents."""
    best: tuple[str, Category, Severity] | None = None
    for prefix, category, severity in _RUFF_CATEGORIES:
        if code.startswith(prefix) and (best is None or len(prefix) > len(best[0])):
            best = (prefix, category, severity)
    if best is None:
        return Category.MAINTAINABILITY, Severity.MINOR
    return best[1], best[2]


def classify_semgrep(severity: str, rule_id: str) -> tuple[Category, Severity]:
    mapped = {
        "ERROR": Severity.MAJOR,
        "WARNING": Severity.MINOR,
        "INFO": Severity.INFO,
    }.get(severity.upper(), Severity.MINOR)

    lowered = rule_id.lower()
    if any(word in lowered for word in ("security", "injection", "xss", "csrf", "crypto", "audit")):
        # Semgrep's security rules are its strongest signal; do not soften them.
        return Category.SECURITY, max(mapped, Severity.MAJOR, key=lambda s: s.rank)
    if "performance" in lowered:
        return Category.PERFORMANCE, mapped
    if "correctness" in lowered or "bug" in lowered:
        return Category.BUG, mapped
    return Category.MAINTAINABILITY, mapped


def classify_eslint(severity: int, rule_id: str) -> tuple[Category, Severity]:
    lowered = (rule_id or "").lower()
    if "security" in lowered or lowered.startswith(("no-eval", "no-implied-eval")):
        return Category.SECURITY, Severity.MAJOR
    if lowered.startswith(("no-unused", "prefer-", "sort-")) or "style" in lowered:
        return Category.STYLE, Severity.INFO
    # eslint severity 2 is "error", 1 is "warn".
    return Category.BUG, Severity.MAJOR if severity >= 2 else Severity.MINOR


def kind_for(severity: Severity) -> Kind:
    """Below major, a static finding reads as a nitpick rather than a real issue."""
    return Kind.POTENTIAL_ISSUE if severity.at_least(Severity.MAJOR) else Kind.NITPICK


def effort_for(severity: Severity) -> Effort:
    return Effort.QUICK_WIN if severity is not Severity.CRITICAL else Effort.MODERATE
