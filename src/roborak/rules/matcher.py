"""Decide which rules apply to a given change.

Only matched rules go into the prompt. That keeps token cost flat as a rule set
grows, and stops a Python rule from being weighed against a Terraform file.
"""

from __future__ import annotations

import fnmatch

from roborak.core.models import ChangeSet
from roborak.rules.loader import Rule


def matching_rules(rules: list[Rule], changeset: ChangeSet) -> list[Rule]:
    """Every rule that applies to at least one changed file, in a stable order."""
    matched = [rule for rule in rules if any(applies_to(rule, f) for f in changeset.files)]
    return sorted(matched, key=lambda r: (-r.severity.rank, r.id))


def applies_to(rule: Rule, file: object) -> bool:
    path = getattr(file, "path", "")
    language = getattr(file, "language", None)

    if rule.languages and language not in rule.languages:
        return False
    if not rule.paths:
        return True
    return any(_matches(path, pattern) for pattern in rule.paths)


def _matches(path: str, pattern: str) -> bool:
    if fnmatch.fnmatch(path, pattern):
        return True
    return pattern.startswith("**/") and fnmatch.fnmatch(path, pattern[3:])


def rules_for_prompt(rules: list[Rule]) -> list[dict[str, str]]:
    """Flatten rules into what the prompt template expects."""
    return [
        {
            "id": rule.qualified_id,
            "severity": rule.severity.value,
            "category": rule.category.value,
            "body": rule.body,
        }
        for rule in rules
    ]
