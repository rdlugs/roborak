"""Machine-readable output.

``--json`` is the full result; ``--agent`` is the same data shaped for another
agent to act on, matching what CodeRabbit's ``--agent`` mode provides: enough to
locate and fix each finding without re-reading the diff.
"""

from __future__ import annotations

import json
from typing import Any

from roborak.core.models import Finding, ReviewResult

SCHEMA_VERSION = 1


def to_dict(result: ReviewResult, *, agent: bool = False) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "summary": {
            "total": len(result.findings),
            "by_severity": {s.value: n for s, n in result.counts_by_severity.items() if n},
            "has_blocking": result.has_blocking,
        },
        "findings": [_finding_dict(f, agent=agent) for f in result.sorted_findings()],
    }

    if not agent:
        # Metadata an agent does not need in order to act.
        payload["model"] = result.model
        payload["skipped_files"] = result.skipped_files
        payload["errors"] = result.errors
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
    }
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
