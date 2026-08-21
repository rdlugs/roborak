"""Mypy.

Type errors are reported as bugs rather than style: mypy only speaks up when a
call cannot succeed as written.
"""

from __future__ import annotations

import json
from pathlib import Path

from roborak.core.models import Finding
from roborak.core.severity import Category, Severity
from roborak.static.adapters.base import Adapter, ToolRun
from roborak.static.normalize import effort_for, kind_for


class MypyAdapter(Adapter):
    name = "mypy"
    binary = "mypy"
    languages = frozenset({"python"})

    def build(self, executable: str, files: list[str], repo: Path) -> ToolRun:
        return ToolRun(
            command=[
                executable,
                "--output=json",
                "--no-error-summary",
                "--no-pretty",
                *files,
            ],
            files=files,
        )

    def parse(self, stdout: str, stderr: str, returncode: int) -> list[Finding]:
        findings: list[Finding] = []
        for line in stdout.splitlines():
            line = line.strip()
            # mypy interleaves plain-text notes with its JSON lines.
            if not line.startswith("{"):
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(entry, dict) or entry.get("severity") == "note":
                continue

            start_line = int(entry.get("line") or 0)
            if not start_line:
                continue
            code = str(entry.get("code") or "")
            severity = Severity.MAJOR if entry.get("severity") == "error" else Severity.MINOR

            body = str(entry.get("message") or "").strip()
            if hint := entry.get("hint"):
                body = f"{body}\n\n{hint}"

            findings.append(
                Finding(
                    file=str(entry.get("file") or ""),
                    start_line=start_line,
                    end_line=int(entry.get("end_line") or start_line),
                    severity=severity,
                    category=Category.BUG,
                    kind=kind_for(severity),
                    effort=effort_for(severity),
                    title=f"Type error: {code}" if code else "Type error",
                    body=body,
                    rule_id=f"mypy/{code}" if code else None,
                    confidence=0.9,
                    source="static",
                    tool="mypy",
                )
            )
        return findings
