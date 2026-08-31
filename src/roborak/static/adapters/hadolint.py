"""hadolint: Dockerfile linting.

Language-selected, because ``detect_language`` now recognises a Dockerfile by
name -- see ``context.diff._LANG_BY_NAME``.
"""

from __future__ import annotations

import json
from pathlib import Path

from roborak.core.models import Finding
from roborak.core.severity import Category, Effort, Kind, Severity
from roborak.static.adapters.base import Adapter, ToolRun

_LEVELS: dict[str, Severity] = {
    "error": Severity.MAJOR,
    "warning": Severity.MINOR,
    "info": Severity.INFO,
    "style": Severity.INFO,
}

_SECURITY_RULES = frozenset({"DL3002", "DL3004", "DL3006", "DL3007", "DL4006", "SC2086"})
"""Rules that describe a trust problem rather than a style one: running as root,
``sudo``, an unpinned or ``latest`` base image, an unguarded pipe in a ``RUN``."""


class HadolintAdapter(Adapter):
    name = "hadolint"
    binary = "hadolint"
    languages = frozenset({"dockerfile"})

    def build(self, executable: str, files: list[str], repo: Path) -> ToolRun:
        return ToolRun(command=[executable, "--format", "json", "--no-color", *files], files=files)

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
            code = str(entry.get("code") or "")
            severity = _LEVELS.get(str(entry.get("level") or "").lower(), Severity.MINOR)
            security = code in _SECURITY_RULES
            if security:
                severity = max(severity, Severity.MAJOR, key=lambda s: s.rank)
            line = int(entry.get("line") or 1)
            findings.append(
                Finding(
                    file=str(entry.get("file") or ""),
                    start_line=line,
                    end_line=line,
                    severity=severity,
                    category=Category.SECURITY if security else Category.MAINTAINABILITY,
                    kind=Kind.POTENTIAL_ISSUE if security else Kind.NITPICK,
                    effort=Effort.QUICK_WIN,
                    title=code or "Dockerfile issue",
                    body=str(entry.get("message") or "").strip(),
                    rule_id=f"hadolint/{code}" if code else None,
                    confidence=0.9,
                    source="static",
                    tool="hadolint",
                )
            )
        return findings
