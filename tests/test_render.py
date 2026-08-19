"""Output renderers.

The machine-readable modes have a contract other tools depend on, so their shape
is asserted explicitly rather than snapshotted.
"""

from __future__ import annotations

import json

import pytest

from roborak.core.models import (
    ChangedFile,
    ChangeSet,
    FileSummary,
    Finding,
    ReviewResult,
    Walkthrough,
)
from roborak.core.severity import Category, Effort, Kind, Severity
from roborak.render import json_out, markdown, prompt_only


def make_result(*, walkthrough: bool = False) -> ReviewResult:
    findings = [
        Finding(
            file="app/auth.py",
            start_line=11,
            end_line=13,
            severity=Severity.CRITICAL,
            category=Category.SECURITY,
            kind=Kind.POTENTIAL_ISSUE,
            effort=Effort.QUICK_WIN,
            title="SQL injection",
            body="user_id is concatenated into SQL.",
            suggestion="row = db.execute('...', (user_id,))",
            confidence=0.95,
        ),
        Finding(
            file="app/util.py",
            start_line=4,
            end_line=4,
            severity=Severity.MINOR,
            category=Category.MAINTAINABILITY,
            kind=Kind.NITPICK,
            title="Unused import",
            body="os is imported but never used.",
            source="static",
            tool="ruff",
            rule_id="ruff/F401",
        ),
    ]
    result = ReviewResult(
        findings=findings,
        model="test/model",
        changeset=ChangeSet(
            files=[ChangedFile(path="app/auth.py"), ChangedFile(path="app/util.py")],
            title="Add session lookup",
            base_ref="main",
            head_ref="feature",
        ),
        skipped_files=["generated/big.ts"],
    )
    if walkthrough:
        result.walkthrough = Walkthrough(
            title="Add session lookup",
            overview="Introduces a session cache keyed by user id.",
            file_summaries=[
                FileSummary(path="app/auth.py", summary="Adds get_session with a | pipe in it"),
            ],
            sequence_diagram="sequenceDiagram\n  Client->>API: GET /session",
            labels=["feature", "security"],
            estimated_effort=3,
        )
    return result


# -- JSON ------------------------------------------------------------------


def test_json_is_valid_and_sorted_by_severity():
    payload = json.loads(json_out.render(make_result()))
    assert payload["schema_version"] == json_out.SCHEMA_VERSION
    assert payload["summary"] == {
        "total": 2,
        "by_severity": {"critical": 1, "minor": 1},
        "has_blocking": True,
    }
    assert [f["severity"] for f in payload["findings"]] == ["critical", "minor"]
    assert payload["model"] == "test/model"
    assert payload["skipped_files"] == ["generated/big.ts"]
    assert payload["changeset"]["head_ref"] == "feature"


def test_json_findings_carry_provenance():
    payload = json.loads(json_out.render(make_result()))
    static = payload["findings"][1]
    assert static["source"] == "static"
    assert static["tool"] == "ruff"
    assert static["rule_id"] == "ruff/F401"
    assert len(static["fingerprint"]) == 16


def test_agent_mode_is_a_lean_actionable_payload():
    payload = json.loads(json_out.render(make_result(), agent=True))
    assert set(payload) == {"schema_version", "summary", "findings"}
    finding = payload["findings"][0]
    # Everything needed to make the fix, and nothing else.
    assert set(finding) == {
        "file",
        "start_line",
        "end_line",
        "severity",
        "category",
        "kind",
        "title",
        "body",
        "suggestion",
    }


def test_json_includes_the_walkthrough_when_there_is_one():
    payload = json.loads(json_out.render(make_result(walkthrough=True)))
    assert payload["walkthrough"]["estimated_effort"] == 3
    assert payload["walkthrough"]["labels"] == ["feature", "security"]


def test_empty_result_is_still_valid_json():
    payload = json.loads(json_out.render(ReviewResult()))
    assert payload["summary"]["total"] == 0
    assert payload["findings"] == []
    assert not payload["summary"]["has_blocking"]


# -- prompt-only -----------------------------------------------------------


def test_prompt_only_is_actionable_text():
    text = prompt_only.render(make_result())
    assert text.startswith("Fix each finding below, starting with the critical ones.")
    assert "Found 1 critical, 1 minor." in text
    assert "1. app/auth.py:11-13" in text
    assert "2. app/util.py:4" in text
    assert "fix: replace those lines with:" in text


def test_prompt_only_flattens_multiline_bodies():
    result = make_result()
    result.findings[0].body = "Line one.\n\nLine two."
    assert "detail: Line one. Line two." in prompt_only.render(result)


def test_prompt_only_when_clean():
    assert prompt_only.render(ReviewResult()) == "No findings."


def test_prompt_only_without_criticals_has_a_calmer_instruction():
    result = make_result()
    result.findings = [result.findings[1]]
    assert prompt_only.render(result).startswith("Fix each finding below. Line numbers")


# -- markdown --------------------------------------------------------------


def test_markdown_structure():
    text = markdown.render(make_result())
    assert text.startswith("# Add session lookup")
    assert "`feature` → `main`" in text
    assert "### 2 findings" in text
    assert "| 🔴 Critical | 1 |" in text
    assert "## 🔴 Critical" in text
    assert "### SQL injection" in text
    assert "```suggestion" in text
    assert "test/model" in text


def test_markdown_reports_skipped_files():
    assert "generated/big.ts" in markdown.render(make_result())


def test_markdown_walkthrough_and_diagram():
    text = markdown.render(make_result(walkthrough=True))
    assert "### Walkthrough" in text
    assert "```mermaid" in text
    assert "sequenceDiagram" in text
    assert "review effort 3/5" in text
    assert "`feature` `security`" in text


def test_markdown_escapes_pipes_in_table_cells():
    """A summary containing a pipe must not break out of its cell."""
    text = markdown.render(make_result(walkthrough=True))
    row = next(line for line in text.splitlines() if line.startswith("| `app/auth.py`"))
    assert "\\|" in row, "the pipe in the summary must be escaped"
    # Only the three structural pipes may be unescaped, or the cell splits in two.
    unescaped = row.replace("\\|", "").count("|")
    assert unescaped == 3, f"pipe leaked into the table: {row}"


def test_markdown_when_clean():
    text = markdown.render(ReviewResult())
    assert "No findings" in text
    assert text.endswith("\n")


def test_markdown_groups_each_severity_once():
    result = make_result()
    result.findings.append(
        Finding(
            file="app/other.py",
            start_line=1,
            end_line=1,
            severity=Severity.CRITICAL,
            category=Category.BUG,
            title="Another critical",
            body="Also bad.",
        )
    )
    text = markdown.render(result)
    assert text.count("## 🔴 Critical") == 1


@pytest.mark.parametrize("renderer", [json_out.render, prompt_only.render, markdown.render])
def test_renderers_survive_an_empty_result(renderer):
    assert renderer(ReviewResult())
