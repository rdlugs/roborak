"""PHPStan.

Reports at whatever level the project's ``phpstan.neon`` sets, for the same
reason as ruff: the configured level is the standard the team actually holds
itself to.
"""

from __future__ import annotations

import json
from pathlib import Path

from roborak.core.models import ChangedFile, Finding
from roborak.core.severity import Category, Severity
from roborak.static.adapters.base import Adapter, ToolRun
from roborak.static.normalize import effort_for, kind_for

CONFIG_CANDIDATES = ("phpstan.neon", "phpstan.neon.dist", "phpstan.dist.neon")


class PhpstanAdapter(Adapter):
    name = "phpstan"
    binary = "phpstan"
    languages = frozenset({"php"})

    def is_available(self, repo: Path, files: list[ChangedFile]) -> bool:
        has_config = any((repo / name).exists() for name in CONFIG_CANDIDATES)
        return super().is_available(repo, files) and has_config

    def build(self, executable: str, files: list[str], repo: Path) -> ToolRun:
        return ToolRun(
            command=[
                executable,
                "analyse",
                "--error-format=json",
                "--no-progress",
                "--no-interaction",
                *files,
            ],
            files=files,
        )

    def parse(self, stdout: str, stderr: str, returncode: int) -> list[Finding]:
        # PHPStan prints its JSON last; anything before it is progress chatter.
        start = stdout.find("{")
        if start < 0:
            return []
        try:
            payload = json.loads(stdout[start:])
        except json.JSONDecodeError:
            return []
        if not isinstance(payload, dict):
            return []

        findings: list[Finding] = []
        for path, detail in (payload.get("files") or {}).items():
            for message in (detail or {}).get("messages") or []:
                if not isinstance(message, dict):
                    continue
                line = message.get("line")
                if not line:
                    continue  # file-level messages have nowhere to anchor
                severity = Severity.MAJOR if not message.get("ignorable", True) else Severity.MINOR
                identifier = str(message.get("identifier") or "")

                findings.append(
                    Finding(
                        file=str(path),
                        start_line=int(line),
                        end_line=int(line),
                        severity=severity,
                        category=Category.BUG,
                        kind=kind_for(severity),
                        effort=effort_for(severity),
                        title=f"PHPStan: {identifier}" if identifier else "PHPStan finding",
                        body=str(message.get("message") or "").strip(),
                        rule_id=f"phpstan/{identifier}" if identifier else None,
                        confidence=0.9,
                        source="static",
                        tool="phpstan",
                    )
                )
        return findings
