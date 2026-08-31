"""osv-scanner: known vulnerabilities in resolved dependencies.

The one adapter here that reaches the network, so ``requires_network`` keeps it
out of autodetection entirely: it runs only when a project puts ``osv-scanner`` in
``static.tools``, which is a person deciding that this review may make an outbound
request. Everything else roborak does stays offline by default, and this adapter
does not get to quietly change that.

It is also the only adapter pointed at a lockfile. That is deliberate and is not a
contradiction of ``ignore_paths``: the file is read by the scanner on disk and
never enters the prompt.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from roborak.core.models import Finding
from roborak.core.severity import Category, Effort, Kind, Severity
from roborak.static.adapters.base import Adapter, ToolRun


class OsvScannerAdapter(Adapter):
    name = "osv-scanner"
    binary = "osv-scanner"
    requires_network = True
    paths = (
        "**/package-lock.json",
        "**/yarn.lock",
        "**/pnpm-lock.yaml",
        "**/uv.lock",
        "**/poetry.lock",
        "**/requirements*.txt",
        "**/go.sum",
        "**/Cargo.lock",
        "**/composer.lock",
    )

    def build(self, executable: str, files: list[str], repo: Path) -> ToolRun:
        command = [executable, "--format", "json"]
        for path in files:
            command += ["--lockfile", path]
        return ToolRun(command=command, files=files)

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
            source = result.get("source")
            path = str(source.get("path") or "") if isinstance(source, dict) else ""
            for package in result.get("packages") or []:
                if isinstance(package, dict):
                    findings.extend(self._package_findings(package, path))
        return findings

    def _package_findings(self, package: dict[str, Any], path: str) -> list[Finding]:
        info = package.get("package")
        name = str(info.get("name") or "") if isinstance(info, dict) else ""
        version = str(info.get("version") or "") if isinstance(info, dict) else ""
        findings: list[Finding] = []
        for vulnerability in package.get("vulnerabilities") or []:
            if not isinstance(vulnerability, dict):
                continue
            identifier = str(vulnerability.get("id") or "")
            summary = str(vulnerability.get("summary") or "").strip()
            findings.append(
                Finding(
                    # A lockfile has no meaningful line for a package, and the
                    # runner narrows findings to changed lines -- so this anchors
                    # to line 1 and reaches the reader through the report rather
                    # than as an inline comment.
                    file=path.lstrip("/"),
                    start_line=1,
                    end_line=1,
                    severity=Severity.MAJOR,
                    category=Category.SECURITY,
                    kind=Kind.POTENTIAL_ISSUE,
                    effort=Effort.MODERATE,
                    title=f"{identifier} in {name}"[:60],
                    body=f"{name} {version}: {summary or identifier}".strip(),
                    rule_id=f"osv/{identifier}" if identifier else None,
                    confidence=0.95,
                    source="static",
                    tool="osv-scanner",
                )
            )
        return findings
