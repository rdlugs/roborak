"""actionlint: GitHub Actions workflow linting.

Selected by path rather than by language. A workflow is YAML, and running a
workflow linter over every YAML file in a repository would produce a parse error
for each one.
"""

from __future__ import annotations

import json
from pathlib import Path

from roborak.core.models import Finding
from roborak.core.severity import Category, Effort, Kind, Severity
from roborak.static.adapters.base import Adapter, ToolRun

_SECURITY_KINDS = frozenset({"expression", "shellcheck", "credentials", "permissions"})
"""actionlint rule kinds that describe an exposure rather than a mistake.

``expression`` covers untrusted-input interpolation into a ``run:`` block, which
is the script-injection path that makes workflow review worth doing at all."""


class ActionlintAdapter(Adapter):
    name = "actionlint"
    binary = "actionlint"
    paths = (
        ".github/workflows/*.yml",
        ".github/workflows/*.yaml",
    )

    def build(self, executable: str, files: list[str], repo: Path) -> ToolRun:
        return ToolRun(command=[executable, "-format", "{{json .}}", *files], files=files)

    def parse(self, stdout: str, stderr: str, returncode: int) -> list[Finding]:
        try:
            payload = json.loads(stdout or "[]")
        except json.JSONDecodeError:
            return []
        if not isinstance(payload, list):
            return []
        findings: list[Finding] = []
        for entry in payload:
            if not isinstance(entry, dict):
                continue
            line = int(entry.get("line") or 1)
            kind = str(entry.get("kind") or "")
            security = kind in _SECURITY_KINDS
            severity = Severity.MAJOR if security else Severity.MINOR
            findings.append(
                Finding(
                    file=str(entry.get("filepath") or ""),
                    start_line=line,
                    end_line=line,
                    severity=severity,
                    category=Category.SECURITY if security else Category.MAINTAINABILITY,
                    kind=Kind.POTENTIAL_ISSUE if security else Kind.NITPICK,
                    effort=Effort.QUICK_WIN,
                    title=(kind or "workflow issue").replace("-", " "),
                    body=str(entry.get("message") or "").strip(),
                    rule_id=f"actionlint/{kind}" if kind else None,
                    confidence=0.9,
                    source="static",
                    tool="actionlint",
                )
            )
        return findings
