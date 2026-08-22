"""ESLint.

Uses the project's local install and its own flat/legacy config. A globally
installed eslint almost never resolves a project's plugins, so a project-local
binary is strongly preferred -- ``find_binary`` already looks there first.
"""

from __future__ import annotations

import json
from pathlib import Path

from roborak.core.models import Finding
from roborak.static.adapters.base import Adapter, ToolRun
from roborak.static.normalize import classify_eslint, effort_for, kind_for


class EslintAdapter(Adapter):
    name = "eslint"
    binary = "eslint"
    languages = frozenset({"javascript", "typescript", "vue"})

    def build(self, executable: str, files: list[str], repo: Path) -> ToolRun:
        return ToolRun(
            command=[executable, "--format", "json", "--no-color", *files],
            files=files,
        )

    def parse(self, stdout: str, stderr: str, returncode: int) -> list[Finding]:
        try:
            payload = json.loads(stdout or "[]")
        except json.JSONDecodeError:
            return []
        if not isinstance(payload, list):
            return []

        findings: list[Finding] = []
        for file_result in payload:
            if not isinstance(file_result, dict):
                continue
            path = str(file_result.get("filePath") or "")
            for message in file_result.get("messages") or []:
                if not isinstance(message, dict):
                    continue
                start_line = int(message.get("line") or 0)
                if not start_line:
                    continue
                rule_id = str(message.get("ruleId") or "")
                category, severity = classify_eslint(int(message.get("severity") or 1), rule_id)

                findings.append(
                    Finding(
                        file=path,
                        start_line=start_line,
                        end_line=int(message.get("endLine") or start_line),
                        severity=severity,
                        category=category,
                        kind=kind_for(severity),
                        effort=effort_for(severity),
                        title=rule_id or "ESLint finding",
                        body=str(message.get("message") or "").strip(),
                        rule_id=f"eslint/{rule_id}" if rule_id else None,
                        confidence=1.0,
                        source="static",
                        tool="eslint",
                    )
                )
        return findings
