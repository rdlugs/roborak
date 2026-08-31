"""checkov: infrastructure-as-code policy checks.

Runs entirely from its bundled policy set, so it needs no network -- which is why
it can be autodetected while ``osv-scanner`` cannot.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from roborak.core.models import Finding
from roborak.core.severity import Category, Effort, Kind, Severity
from roborak.static.adapters.base import Adapter, ToolRun


class CheckovAdapter(Adapter):
    name = "checkov"
    binary = "checkov"
    languages = frozenset({"terraform", "dockerfile"})

    def build(self, executable: str, files: list[str], repo: Path) -> ToolRun:
        command = [executable, "--quiet", "--compact", "--output", "json"]
        for path in files:
            command += ["--file", path]
        return ToolRun(command=command, files=files)

    def parse(self, stdout: str, stderr: str, returncode: int) -> list[Finding]:
        # checkov emits a single object for one framework and a list when several
        # matched, and prints nothing at all when every check passed.
        start = stdout.find("{") if stdout else -1
        bracket = stdout.find("[") if stdout else -1
        if bracket != -1 and (start == -1 or bracket < start):
            start = bracket
        if start == -1:
            return []
        try:
            payload = json.loads(stdout[start:])
        except json.JSONDecodeError:
            return []
        reports = payload if isinstance(payload, list) else [payload]

        findings: list[Finding] = []
        for report in reports:
            if not isinstance(report, dict):
                continue
            results = report.get("results")
            checks = results.get("failed_checks") if isinstance(results, dict) else None
            if not isinstance(checks, list):
                continue
            findings.extend(self._finding(check) for check in checks if isinstance(check, dict))
        return [f for f in findings if f.file]

    def _finding(self, check: dict[str, Any]) -> Finding:
        lines = check.get("file_line_range")
        start, end = [*lines, 0, 0][:2] if isinstance(lines, list) else (1, 1)
        start = max(int(start or 1), 1)
        end = max(int(end or start), start)
        check_id = str(check.get("check_id") or "")
        # Every checkov policy is a statement about exposure -- a public bucket,
        # an unencrypted volume, a permissive rule -- so the category is not in
        # question and only how much it should worry a reader is.
        severity = (
            Severity.MAJOR
            if str(check.get("severity") or "").upper()
            in {
                "HIGH",
                "CRITICAL",
            }
            else Severity.MINOR
        )
        return Finding(
            file=str(check.get("file_path") or "").lstrip("/"),
            start_line=start,
            end_line=end,
            severity=severity,
            category=Category.SECURITY,
            kind=Kind.POTENTIAL_ISSUE,
            effort=Effort.MODERATE,
            title=(str(check.get("check_name") or check_id))[:60],
            body=str(check.get("check_name") or "").strip(),
            rule_id=f"checkov/{check_id}" if check_id else None,
            confidence=0.9,
            source="static",
            tool="checkov",
        )
