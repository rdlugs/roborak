"""Machine-readable output.

``--json`` is the full result; ``--agent`` is the same data shaped for another
agent to act on: enough to locate and fix each finding without re-reading the
diff.
"""

from __future__ import annotations

import json
from typing import Any

from roborak.core.models import Finding, ReviewResult
from roborak.core.verdict import gate_for, verdict_requested

SCHEMA_VERSION = 2


def to_dict(result: ReviewResult, *, agent: bool = False) -> dict[str, Any]:
    omitted_paths = {item.path for item in result.coverage}
    reviewed_files = (
        [file.path for file in result.changeset.files if file.path not in omitted_paths]
        if result.changeset is not None
        else []
    )
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "status": result.status.value,
        "errors": result.errors,
        "coverage": {
            "reviewed_files": reviewed_files,
            "omissions": [
                item.model_dump(mode="json", exclude_none=True) for item in result.coverage
            ],
        },
        "summary": {
            "total": len(result.findings),
            "by_severity": {s.value: n for s, n in result.counts_by_severity.items() if n},
            "has_blocking": result.has_blocking,
        },
        "findings": [_finding_dict(f, agent=agent) for f in result.sorted_findings()],
    }

    # Absent rather than assumed: `describe` renders through here too and never
    # judged anything, so a verdict key would be a claim nobody made.
    if verdict_requested(result):
        gate = gate_for(result)
        payload["summary"]["verdict"] = gate.verdict.value
        payload["summary"]["block_on"] = gate.floor.value

    if not agent:
        payload["model"] = result.model
        payload["models_used"] = result.models_used
        payload["tokens_used"] = result.tokens_used
        payload["usage"] = [
            call.model_dump() | {"total_tokens": call.total_tokens} for call in result.usage
        ]
        payload["skipped_files"] = result.skipped_files
        if result.changeset is not None:
            payload["changeset"] = {
                "origin": result.changeset.origin,
                "title": result.changeset.title,
                "base_sha": result.changeset.base_sha,
                "head_sha": result.changeset.head_sha,
                "base_ref": result.changeset.base_ref,
                "head_ref": result.changeset.head_ref,
                "files": [f.path for f in result.changeset.files],
            }
        if result.issue is not None:
            payload["issue"] = result.issue.model_dump(exclude_none=True)
        if result.walkthrough is not None:
            payload["walkthrough"] = result.walkthrough.model_dump(exclude_none=True)

    return payload


def _finding_dict(finding: Finding, *, agent: bool) -> dict[str, Any]:
    data: dict[str, Any] = {
        "file": finding.file,
        "start_line": finding.start_line,
        "end_line": finding.end_line,
        "severity": finding.severity.value,
        "category": finding.category.value,
        "kind": finding.kind.value,
        "title": finding.title,
        "body": finding.body,
        "evidence": finding.evidence.value,
    }
    if finding.evidence_note:
        data["evidence_note"] = finding.evidence_note
    if finding.evidence_files:
        data["evidence_files"] = list(finding.evidence_files)
    if finding.suggestion:
        data["suggestion"] = finding.suggestion
    if agent:
        return data

    data.update(
        {
            "effort": finding.effort.value,
            "confidence": finding.confidence,
            "source": finding.source,
            "rule_id": finding.rule_id,
            "tool": finding.tool,
            "fingerprint": finding.fingerprint,
        }
    )
    return data


def render(result: ReviewResult, *, agent: bool = False) -> str:
    return json.dumps(to_dict(result, agent=agent), indent=2, ensure_ascii=False)
