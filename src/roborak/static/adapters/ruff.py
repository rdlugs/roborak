"""Ruff.

Run with the project's own configuration rather than an imposed rule set: the
repo's selection is the one its team already agreed with, and overriding it
would bury real findings under style noise the project deliberately disabled.
"""

from __future__ import annotations

import json
from pathlib import Path

from roborak.core.models import Finding
from roborak.static.adapters.base import Adapter, ToolRun
from roborak.static.normalize import classify_ruff, effort_for, kind_for


class RuffAdapter(Adapter):
    name = "ruff"
    binary = "ruff"
    languages = frozenset({"python"})

    def build(self, executable: str, files: list[str], repo: Path) -> ToolRun:
        return ToolRun(
            command=[executable, "check", "--output-format", "json", "--quiet", *files],
            files=files,
        )

    def parse(self, stdout: str, stderr: str, returncode: int) -> list[Finding]:
        try:
            entries = json.loads(stdout or "[]")
        except json.JSONDecodeError:
            return []
        if not isinstance(entries, list):
            return []

        findings: list[Finding] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            code = str(entry.get("code") or "")
            location = entry.get("location") or {}
            end = entry.get("end_location") or {}
            start_line = int(location.get("row") or 0)
            if not start_line:
                continue

            category, severity = classify_ruff(code)
            findings.append(
                Finding(
                    file=str(entry.get("filename") or ""),
                    start_line=start_line,
                    end_line=int(end.get("row") or start_line),
                    severity=severity,
                    category=category,
                    kind=kind_for(severity),
                    effort=effort_for(severity),
                    title=f"{code} {entry.get('name') or ''}".strip(),
                    body=_body(entry),
                    rule_id=f"ruff/{code}" if code else None,
                    confidence=1.0,  # a linter match is a fact, not a judgement
                    source="static",
                    tool="ruff",
                )
            )
        return findings


def _body(entry: dict[str, object]) -> str:
    message = str(entry.get("message") or "").strip()
    if url := entry.get("url"):
        return f"{message}\n\n{url}"
    return message
