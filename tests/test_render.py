"""Output renderers.

The machine-readable modes have a contract other tools depend on, so their shape
is asserted explicitly rather than snapshotted.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from roborak.core.models import (
    ChangedFile,
    ChangeSet,
    FileSummary,
    Finding,
    Issue,
    LLMCallUsage,
    ReviewResult,
    Walkthrough,
)
from roborak.core.severity import Category, Effort, Kind, Severity
from roborak.render import json_out, markdown, prompt_only


def test_result_accumulates_usage_metadata():
    result = ReviewResult()
    result.add_usage(
        LLMCallUsage(purpose="review", model="test/model", prompt_tokens=10, completion_tokens=5)
    )
    assert result.tokens_used == 15
    assert result.models_used == ["test/model"]


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
    assert set(payload) == {
        "schema_version",
        "status",
        "errors",
        "coverage",
        "summary",
        "findings",
    }
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
    assert "**SQL injection.**" in text
    assert "```suggestion" in text
    assert "test/model" in text


def test_markdown_buckets_findings_into_collapsible_sections():
    """A review with thirty nitpicks must not bury the two findings that matter."""
    text = markdown.render(make_result())
    assert "<summary>Actionable comments (1)</summary><blockquote>" in text
    assert "<summary>🧹 Nitpick comments (1)</summary><blockquote>" in text
    # Nested details need the blockquote; leaf ones are cleaner without it.
    assert "<summary>app/auth.py (1)</summary><blockquote>" in text
    assert "<summary>🤖 Prompt for AI Agents</summary>\n" in text


def test_a_finding_carries_its_badges_in_order():
    text = markdown.render(make_result())
    assert "`11-13`: _🔒 Security_ | _🔴 Critical_ | _⚡ Quick win_" in text
    assert "_📐 Maintainability & Code Quality_ | _🟡 Minor_ | _🔨 Moderate_" in text


def test_each_finding_carries_an_agent_prompt_and_a_fingerprint():
    result = make_result()
    text = markdown.render(result)
    for finding in result.findings:
        assert f"<!-- roborak:v1:{finding.fingerprint} -->" in text
    assert text.count("<summary>🤖 Prompt for AI Agents</summary>") == 2
    assert "In `@app/auth.py` at lines 11-13," in text


def test_the_global_agent_prompt_collates_every_instruction():
    text = markdown.render(make_result())
    block = text[text.index("Prompt for all review comments") :]
    assert "Actionable comments:\nIn `@app/auth.py`:" in block
    assert "Nitpick comments:\nIn `@app/util.py`:" in block
    assert "- Line 4: os is imported but never used." in block
    # Emoji belong on the rendered badge, not in a block an agent parses.
    assert "🧹" not in block[: block.index("</details>")]


def test_agent_prompts_are_wrapped_to_the_fence_width():
    """A fenced block does not wrap, so an unwrapped line becomes a scrollbar."""
    text = markdown.render(make_result())
    for line in text.splitlines():
        assert len(line) <= markdown.FENCE_WIDTH + 4, line


def test_outside_diff_findings_get_a_caution_banner():
    """The one section that opens itself: an unanchored finding is easily lost."""
    from roborak.core.models import Hunk

    result = make_result()
    result.changeset.files[0] = ChangedFile(
        path="app/auth.py",
        hunks=[
            Hunk(
                old_start=1,
                old_lines=2,
                new_start=1,
                new_lines=2,
                content="",
                line_map={1: 1, 2: 2},
                added_lines={2},
            )
        ],
    )
    text = markdown.render(result)
    assert "> [!CAUTION]" in text
    assert "can't be posted inline" in text
    assert "> <summary>⚠️ Outside diff range comments (1)</summary><blockquote>" in text


def test_markdown_review_info():
    text = markdown.render(make_result())
    info = text[text.index("Review info") :]
    assert "**Model**: `test/model`" in info
    assert "📒 Files selected for processing (2)" in info
    assert "* `app/auth.py`" in info
    assert "🚧 Files skipped (context budget) (1)" in info


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


def test_markdown_groups_a_bucket_by_file():
    """A reviewer reads one file at a time, so each file gets one section."""
    result = make_result()
    result.findings.append(
        Finding(
            file="app/auth.py",
            start_line=20,
            end_line=20,
            severity=Severity.CRITICAL,
            category=Category.BUG,
            title="Another critical",
            body="Also bad.",
        )
    )
    text = markdown.render(result)
    assert text.count("<summary>Actionable comments (2)</summary>") == 1
    assert text.count("<summary>app/auth.py (2)</summary>") == 1
    # Findings within one file are rule-separated.
    assert "\n\n---\n\n" in text


@pytest.mark.parametrize("renderer", [json_out.render, prompt_only.render, markdown.render])
def test_renderers_survive_an_empty_result(renderer):
    assert renderer(ReviewResult())


# -- the issue a review was judged against ---------------------------------


def result_with_an_issue() -> ReviewResult:
    result = make_result()
    result.issue = Issue(
        provider="github",
        host="github.com",
        project="acme/web",
        number=42,
        title="Sessions can be hijacked",
        web_url="https://github.com/acme/web/issues/42",
    )
    return result


def test_markdown_header_links_the_issue():
    body = markdown.render(result_with_an_issue())
    assert "against [#42](https://github.com/acme/web/issues/42)" in body


def test_markdown_says_nothing_when_there_is_no_issue():
    assert "against [#" not in markdown.render(make_result())


def test_json_carries_the_issue():
    payload = json.loads(json_out.render(result_with_an_issue()))
    assert payload["issue"]["number"] == 42
    assert payload["issue"]["title"] == "Sessions can be hijacked"


def test_agent_mode_leaves_the_issue_out():
    # --agent carries only what is needed to act on a finding.
    payload = json.loads(json_out.render(result_with_an_issue(), agent=True))
    assert "issue" not in payload


def test_terminal_footer_names_the_issue():
    from rich.console import Console

    from roborak.render import terminal

    console = Console(record=True, width=100)
    terminal.render(result_with_an_issue(), console, Path("/nonexistent"))
    assert "reviewed against issue #42" in console.export_text()


def test_a_gap_panel_shows_the_file_without_a_line():
    from rich.console import Console

    from roborak.render import terminal

    result = make_result()
    result.findings = [
        Finding(
            file="app/auth.py",
            start_line=1,
            end_line=1,
            severity=Severity.MAJOR,
            category=Category.SECURITY,
            kind=Kind.REQUIREMENT_GAP,
            title="No rate limiting was added",
            body="The issue asks for a rate limit; nothing here adds one.",
        )
    ]
    console = Console(record=True, width=100)
    terminal.render(result, console, Path("/nonexistent"))
    text = console.export_text()

    assert "Requirement gap" in text
    # The line is nominal, so it must not be shown as though it were an anchor.
    assert "app/auth.py:1" not in text
    assert "app/auth.py" in text


# -- terminal header and finding detail ------------------------------------


def render_terminal(result: ReviewResult, width: int = 100) -> str:
    from rich.console import Console

    from roborak.render import terminal

    console = Console(record=True, width=width)
    terminal.render(result, console, Path("/nonexistent"))
    return console.export_text()


def test_terminal_header_says_what_was_reviewed():
    text = render_terminal(make_result())
    assert "Add session lookup" in text
    assert "feature → main" in text
    assert "2 file(s) changed" in text


def test_terminal_header_carries_the_walkthrough():
    text = render_terminal(make_result(walkthrough=True), width=120)
    assert "Introduces a session cache keyed by user id." in text
    assert "app/auth.py" in text
    assert "review effort 3/5" in text
    # Raw mermaid is unreadable in a terminal; the markdown report carries it.
    assert "sequenceDiagram" not in text


def test_terminal_header_survives_a_review_with_no_walkthrough():
    result = make_result()
    assert result.walkthrough is None
    assert "Add session lookup" in render_terminal(result)


def test_terminal_stays_quiet_when_there_is_nothing_to_review():
    result = ReviewResult(changeset=ChangeSet(files=[]))
    text = render_terminal(result)
    assert "No changes to review" in text
    # A header announcing "0 file(s) changed" would be noise.
    assert "file(s) changed" not in text


def test_terminal_findings_carry_the_same_badges_as_the_report():
    text = render_terminal(make_result(), width=120)
    assert "🔒 Security │ 🔴 Critical │ ⚡ Quick win" in text
    assert "confidence 95%" in text


def test_a_static_finding_reports_no_confidence():
    """Only the model calibrates one; a linter's default would be a number we invented."""
    result = make_result()
    static = next(f for f in result.findings if f.source == "static")
    static.kind = Kind.POTENTIAL_ISSUE  # a nitpick renders compact, without provenance
    result.findings = [static]
    text = render_terminal(result, width=120)
    assert "via ruff" in text
    assert "confidence" not in text


def test_nitpicks_are_compressed_to_one_line_each():
    """The terminal cannot collapse a section, so it shrinks it instead."""
    result = make_result()
    text = render_terminal(result, width=120)
    assert "🧹 Nitpick comments (1)" in text
    assert "Actionable comments (1)" in text
    assert "• app/util.py:4  Unused import" in text
    # The nitpick gets no code snippet; the critical finding still does.
    assert "os is imported but never used." not in text


def test_the_terminal_summary_does_not_claim_anything_was_posted():
    text = render_terminal(make_result())
    assert "2 findings" in text
    assert "posted" not in text


# -- the terminal form of the same document --------------------------------


def test_the_terminal_form_turns_sections_into_headings():
    """rich.Markdown drops HTML silently, taking every section heading with it."""
    text = markdown.render(make_result(), collapsible=False)
    assert "<details>" not in text
    assert "<summary>" not in text
    assert "<blockquote>" not in text
    assert "## Actionable comments (1)" in text
    assert "### app/auth.py (1)" in text
    assert "#### 🤖 Prompt for AI Agents" in text
    assert "## ℹ️ Review info" in text


def test_both_forms_say_the_same_things():
    """Only the way a section folds may differ; the words may not."""
    result = make_result(walkthrough=True)
    published = markdown.render(result)
    shown = markdown.render(result, collapsible=False)

    for fragment in (
        "**SQL injection.**",
        "user_id is concatenated into SQL.",
        "**Unused import.**",
        "_🔒 Security_ | _🔴 Critical_ | _⚡ Quick win_",
        "Introduces a session cache keyed by user id.",
        "Actionable comments:\nIn `@app/auth.py`:",
        "_Confidence: 95%_",
    ):
        assert fragment in published, fragment
        assert fragment in shown, fragment

    # The identity markers survive too; rich hides them as HTML comments.
    for finding in result.findings:
        assert f"<!-- roborak:v1:{finding.fingerprint} -->" in shown


def test_the_terminal_names_the_language_so_the_fix_gets_coloured():
    """A forge turns ```suggestion into a one-click apply; a terminal cannot,
    and would render it as an uncoloured block."""
    result = make_result()
    assert "```suggestion" in markdown.render(result)
    assert "```python" in markdown.render(result, collapsible=False)


def test_the_caution_banner_becomes_a_plain_note():
    from roborak.core.models import Hunk

    result = make_result()
    result.changeset.files[0] = ChangedFile(
        path="app/auth.py",
        hunks=[
            Hunk(
                old_start=1,
                old_lines=2,
                new_start=1,
                new_lines=2,
                content="",
                line_map={1: 1, 2: 2},
                added_lines={2},
            )
        ],
    )
    shown = markdown.render(result, collapsible=False)
    # rich shows `[!CAUTION]` as literal text, and quoting the whole section is
    # a wall of bar -- but the warning itself must survive.
    assert "[!CAUTION]" not in shown
    assert "> Some comments are outside the diff" in shown
    assert "## ⚠️ Outside diff range comments (1)" in shown


def test_the_terminal_form_renders_through_rich_without_losing_a_heading():
    from rich.console import Console
    from rich.markdown import Markdown

    console = Console(record=True, width=100)
    console.print(Markdown(markdown.render(make_result(), collapsible=False)))
    text = console.export_text()

    assert "Actionable comments (1)" in text
    assert "🧹 Nitpick comments (1)" in text
    assert "SQL injection." in text
    assert "roborak:v1" not in text, "the identity marker is for machines, not readers"
