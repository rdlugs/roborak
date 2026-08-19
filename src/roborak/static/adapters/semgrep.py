"""Semgrep.

Only run when the repo ships its own rules. ``--config auto`` would reach out to
the registry, which is both slow and a surprise for anyone reviewing offline or
on a private codebase, so roborak never does it on the user's behalf.
"""

from __future__ import annotations

import json
from pathlib import Path

from roborak.core.models import ChangedFile, Finding
from roborak.static.adapters.base import Adapter, ToolRun
from roborak.static.normalize import classify_semgrep, effort_for, kind_for

CONFIG_CANDIDATES = (".semgrep.yml", ".semgrep.yaml", "semgrep.yml", "semgrep.yaml", ".semgrep")


class SemgrepAdapter(Adapter):
    name = "semgrep"
    binary = "semgrep"
    languages = frozenset()  # semgrep covers many languages

    def config_path(self, repo: Path) -> Path | None:
        return next((repo / name for name in CONFIG_CANDIDATES if (repo / name).exists()), None)

    def is_available(self, repo: Path, files: list[ChangedFile]) -> bool:
        return super().is_available(repo, files) and self.config_path(repo) is not None

    def build(self, executable: str, files: list[str], repo: Path) -> ToolRun:
        config = self.config_path(repo)
        return ToolRun(
            command=[
                executable,
                "--json",
                "--quiet",
                "--no-git-ignore",
                "--config",
                str(config),
                *files,
            ],
            files=files,
        )

    def parse(self, stdout: str, stderr: str, returncode: int) -> list[Finding]:
        try:
            payload = json.loads(stdout or "{}")
        except json.JSONDecodeError:
            return []
        if not isinstance(payload, dict):
            return []

        findings: list[Finding] = []
        for result in payload.get("results") or []:
            if not isinstance(result, dict):
                continue
            start = (result.get("start") or {}).get("line")
            if not start:
                continue
            extra = result.get("extra") or {}
            rule_id = str(result.get("check_id") or "")
            category, severity = classify_semgrep(str(extra.get("severity") or ""), rule_id)

            findings.append(
                Finding(
                    file=str(result.get("path") or ""),
                    start_line=int(start),
                    end_line=int((result.get("end") or {}).get("line") or start),
                    severity=severity,
                    category=category,
                    kind=kind_for(severity),
                    effort=effort_for(severity),
                    title=rule_id.rsplit(".", 1)[-1].replace("-", " ") or "Semgrep finding",
                    body=str(extra.get("message") or "").strip(),
                    rule_id=f"semgrep/{rule_id}" if rule_id else None,
                    confidence=0.95,
                    source="static",
                    tool="semgrep",
                )
            )
        return findings
