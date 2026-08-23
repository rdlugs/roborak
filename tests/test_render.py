"""Output renderers.

The machine-readable modes have a contract other tools depend on, so their shape
is asserted explicitly rather than snapshotted.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from rich.console import Console

from roborak.core.models import (
    ChangedFile,
    ChangeSet,
    FileSummary,
    Finding,
    Issue,
    LLMCallUsage,
    ReviewComment,
    ReviewResult,
    Walkthrough,
)
from roborak.core.severity import Category, Effort, Kind, Severity
from roborak.core.verdict import Verdict
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


def test_json_is_valid_and_sorted_by_severity():
    result = make_result()
    assert result.changeset is not None
    result.changeset.discussions = [ReviewComment(author="sam", body="Private context")]
    payload = json.loads(json_out.render(result))
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
    assert "discussions" not in payload["changeset"]


def test_json_keeps_model_usage_metadata():
    result = make_result()
    result.add_usage(
        LLMCallUsage(purpose="review", model="test/model", prompt_tokens=10, completion_tokens=5)
    )

    payload = json.loads(json_out.render(result))

    assert payload["model"] == "test/model"
    assert payload["models_used"] == ["test/model"]
    assert payload["tokens_used"] == 15
    assert payload["usage"][0]["total_tokens"] == 15


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


def test_markdown_structure():
    text = markdown.render(make_result())
    assert text.startswith("# Add session lookup")
    assert "`feature` → `main`" in text
    assert "### 2 findings" in text
    assert "| 🔴 Critical | 1 |" in text
    assert "**SQL injection.**" in text
    assert "```suggestion" in text
    assert "test/model" not in text
    assert "<!-- roborak:review -->" in text


def test_markdown_buckets_findings_into_collapsible_sections():
    """A review with thirty nitpicks must not bury the two findings that matter."""
    text = markdown.render(make_result())
    assert "<summary>Actionable comments (1)</summary><blockquote>" in text
    assert "<summary>🧹 Nitpick comments (1)</summary><blockquote>" in text
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
    assert "🧹" not in block[: block.index("</details>")]


def test_agent_prompts_are_wrapped_to_the_fence_width():
    """A fenced block does not wrap, so an unwrapped line becomes a scrollbar."""
    text = markdown.render(make_result())
    signature = markdown._signature(form=markdown.Form.PUBLISHED)
    for line in text.splitlines():
        if line == signature:
            continue
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
    result = make_result()
    result.add_usage(
        LLMCallUsage(purpose="review", model="test/model", prompt_tokens=10, completion_tokens=5)
    )
    text = markdown.render(result)
    info = text[text.index("Review info") :]
    assert "**Model**" not in info
    assert "Model usage" not in info
    assert "test/model" not in info
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


def test_markdown_renders_a_general_mermaid_flow():
    result = make_result(walkthrough=True)
    assert result.walkthrough is not None
    result.walkthrough.sequence_diagram = "flowchart TD\n  Boot --> Routes"

    text = markdown.render(result)

    assert "### Flow" in text
    assert "```mermaid\nflowchart TD" in text


def test_markdown_escapes_pipes_in_table_cells():
    """A summary containing a pipe must not break out of its cell."""
    text = markdown.render(make_result(walkthrough=True))
    row = next(line for line in text.splitlines() if line.startswith("| `app/auth.py`"))
    assert "\\|" in row, "the pipe in the summary must be escaped"
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
    assert "\n\n---\n\n" in text


@pytest.mark.parametrize("renderer", [json_out.render, prompt_only.render, markdown.render])
def test_renderers_survive_an_empty_result(renderer):
    assert renderer(ReviewResult())


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
    assert "app/auth.py:1" not in text
    assert "app/auth.py" in text


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
    assert "test/model" not in text


def test_terminal_header_carries_the_walkthrough():
    text = render_terminal(make_result(walkthrough=True), width=120)
    assert "Introduces a session cache keyed by user id." in text
    assert "app/auth.py" in text
    assert "review effort 3/5" in text
    assert "sequenceDiagram" not in text


def test_terminal_header_survives_a_review_with_no_walkthrough():
    result = make_result()
    assert result.walkthrough is None
    assert "Add session lookup" in render_terminal(result)


def test_terminal_stays_quiet_when_there_is_nothing_to_review():
    result = ReviewResult(changeset=ChangeSet(files=[]))
    text = render_terminal(result)
    assert "No changes to review" in text
    assert "file(s) changed" not in text


def test_terminal_findings_carry_the_same_badges_as_the_report():
    text = render_terminal(make_result(), width=120)
    assert "🔒 Security │ 🔴 Critical │ ⚡ Quick win" in text
    assert "confidence 95%" in text


def test_a_static_finding_reports_no_confidence():
    """Only the model calibrates one; a linter's default would be a number we invented."""
    result = make_result()
    static = next(f for f in result.findings if f.source == "static")
    static.kind = Kind.POTENTIAL_ISSUE
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
    assert "os is imported but never used." not in text


def test_the_terminal_summary_does_not_claim_anything_was_posted():
    text = render_terminal(make_result())
    assert "2 findings" in text
    assert "posted" not in text


def _terminal(result, **kwargs) -> str:
    return markdown.render(result, form=markdown.Form.TERMINAL, **kwargs)


def test_the_terminal_form_turns_sections_into_headings():
    """rich.Markdown drops HTML silently, taking every section heading with it."""
    text = _terminal(make_result())
    assert "<details>" not in text
    assert "<summary>" not in text
    assert "<blockquote>" not in text
    assert "## Actionable comments (1)" in text
    assert "### app/auth.py (1)" in text


def test_the_terminal_leaves_out_what_is_written_for_a_machine():
    """Opened out, the agent prompts and the review-info tree bury the review."""
    text = _terminal(make_result())
    assert "🤖 Prompt for AI Agents" not in text
    assert "🤖 Prompt for all review comments" not in text
    assert "ℹ️ Review info" not in text
    assert "📒 Files selected for processing" not in text


def test_full_puts_the_machine_sections_back():
    text = _terminal(make_result(), full=True)
    assert "#### 🤖 Prompt for AI Agents" in text
    assert "## 🤖 Prompt for all review comments with AI agents" in text
    assert "## ℹ️ Review info" in text


def test_the_terminal_footer_carries_the_run_without_the_tree():
    text = _terminal(make_result())
    assert "source local" in text
    assert "model" not in text, "model metadata is not for a human-facing report"
    assert "--full" in text, "the reader has to be told the rest is one flag away"


def test_the_footer_never_hides_what_was_not_reviewed():
    """A skipped file is the one thing a folded section must not be able to lose."""
    text = _terminal(make_result())
    assert "generated/big.ts" in text

    result = make_result()
    result.errors.append("chunk 2 failed")
    assert "chunk 2 failed" in _terminal(result)


def test_the_footer_counts_the_files_it_does_not_name():
    result = make_result()
    result.skipped_files = [f"gen/{n}.ts" for n in range(8)]
    text = _terminal(result)
    assert "gen/0.ts" in text
    assert "and 3 more" in text
    assert "gen/7.ts" not in text, "a footer that listed them all would not be a footer"


def test_a_finding_leads_with_a_path_an_editor_can_open():
    """The file is three headings up the screen; the finding has to say it again."""
    text = _terminal(make_result())
    assert "app/auth.py:11-13" in text
    assert "##### 🔒 Security · 🔴 Critical · ⚡ Quick win · app/auth.py:11-13" in text


def test_a_gap_leads_with_the_file_and_no_line(tmp_path: Path):
    result = make_result()
    result.findings = [
        Finding(
            file="app/auth.py",
            start_line=1,
            end_line=1,
            severity=Severity.MAJOR,
            category=Category.LOGIC,
            kind=Kind.REQUIREMENT_GAP,
            title="No rate limit",
            body="The issue asked for one.",
        )
    ]
    (tmp_path / "app").mkdir()
    (tmp_path / "app/auth.py").write_text("x = 1\n")

    text = _terminal(result, repo=tmp_path)
    assert "· app/auth.py" in text
    assert "app/auth.py:1" not in text, "a gap has no line to point at"
    assert markdown.CONTEXT_FENCE not in text, "nor any code to show"


def test_a_finding_carries_the_lines_it_points_at(tmp_path: Path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app/auth.py").write_text("\n".join(f"line {n}" for n in range(1, 21)) + "\n")

    text = _terminal(make_result(), repo=tmp_path)
    assert f"```{markdown.CONTEXT_FENCE} 8 11 13 app/auth.py" in text
    assert "line 8" in text, "three lines of context above"
    assert "line 16" in text, "and three below"
    assert "line 7" not in text


def test_a_long_span_is_shown_from_the_top_not_in_full(tmp_path: Path):
    """One finding must not be able to cost a screenful."""
    from roborak.render import snippet

    (tmp_path / "app").mkdir()
    (tmp_path / "app/auth.py").write_text("\n".join(f"line {n}" for n in range(1, 101)) + "\n")

    result = make_result()
    result.findings[0].end_line = 60
    text = _terminal(result, repo=tmp_path)

    block = text.split(f"```{markdown.CONTEXT_FENCE}")[1].split("```")[0]
    code = block.strip().splitlines()[1:]
    assert len(code) == snippet.MAX_LINES
    assert code[0] == "line 8" and code[-1] == "line 21"
    assert "app/auth.py:11-60" in text, "the lead still names the whole span"


def test_no_repo_and_no_file_mean_no_code_block(tmp_path: Path):
    """A forge review has no working tree, and a deleted file has no lines."""
    assert markdown.CONTEXT_FENCE not in _terminal(make_result())
    assert markdown.CONTEXT_FENCE not in _terminal(make_result(), repo=tmp_path)


def test_no_finding_is_lost_between_the_forms():
    """The terminal may show less *about* the run. It may not show less review."""
    result = make_result(walkthrough=True)
    published = markdown.render(result)
    shown = _terminal(result)

    for fragment in (
        "**SQL injection.**",
        "user_id is concatenated into SQL.",
        "**Unused import.**",
        "os is imported but never used.",
        "🔒 Security",
        "🔴 Critical",
        "⚡ Quick win",
        "Introduces a session cache keyed by user id.",
        "_Confidence: 95%_",
        "row = db.execute",
    ):
        assert fragment in published, fragment
        assert fragment in shown, fragment

    for finding in result.findings:
        assert f"<!-- roborak:v1:{finding.fingerprint} -->" in published
    assert "roborak:v1" not in shown


def test_the_terminal_names_the_language_so_the_fix_gets_coloured():
    """A forge turns ```suggestion into a one-click apply; a terminal cannot,
    and would render it as an uncoloured block."""
    result = make_result()
    assert "```suggestion" in markdown.render(result)
    assert "```python" in _terminal(result)


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
    shown = _terminal(result)
    assert "[!CAUTION]" not in shown
    assert "> Some comments are outside the diff" in shown
    assert "## ⚠️ Outside diff range comments (1)" in shown


def render_report(result, repo: Path | None = None, *, full: bool = False) -> Console:
    from roborak.render.rich_report import ReportMarkdown

    console = Console(record=True, width=100)
    console.print(
        ReportMarkdown(markdown.render(result, form=markdown.Form.TERMINAL, repo=repo, full=full))
    )
    return console


def test_the_report_renders_through_rich_without_losing_a_heading():
    text = render_report(make_result()).export_text()

    assert "Actionable comments (1)" in text
    assert "🧹 Nitpick comments (1)" in text
    assert "SQL injection." in text
    assert "app/auth.py:11-13" in text
    assert "roborak:v1" not in text, "the identity marker is for machines, not readers"
    assert "🤖 Prompt" not in text


def test_the_rendered_report_colours_a_finding_by_its_severity():
    """The badge says critical; the line has to look it.

    The one thing the report gains over the document it renders. Asserted on the
    escape codes because "it is red" is the whole feature.
    """
    styled = render_report(make_result()).export_text(styles=True)
    critical = next(line for line in styled.splitlines() if "🔒 Security" in line)
    minor = next(line for line in styled.splitlines() if "📐 Maintainability" in line)

    assert critical.startswith("\x1b[") and ";31m" in critical.split("m")[0] + "m", critical
    assert ";31m" not in minor.split("m")[0] + "m", "a nitpick is not red"
    assert "\x1b[36m" in minor or ";36m" in minor, "SEVERITY_STYLE[MINOR] is cyan"


def test_the_rendered_report_gutters_the_code_it_shows(tmp_path: Path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app/auth.py").write_text("\n".join(f"line {n}" for n in range(1, 21)) + "\n")

    text = render_report(make_result(), tmp_path).export_text()
    assert markdown.CONTEXT_FENCE not in text, "the sentinel is for the renderer, not the reader"
    assert " 11 " in text, "the gutter numbers from the file's real position"
    assert "line 11" in text


def test_the_published_report_is_signed_with_the_icon():
    """A posted comment should say who wrote it where a reader can see it."""
    text = markdown.render(make_result())
    assert markdown.LOGO_URL in text
    assert "Reviewed by <b>roborak</b>" in text
    assert text.rstrip().endswith("</sub>"), "the signature is the last thing in the document"


def test_the_terminal_report_is_signed_without_html():
    """rich.Markdown drops HTML, so the terminal form signs itself in characters."""
    text = _terminal(make_result())
    assert "Reviewed by roborak" in text
    assert "<img" not in text
    assert "<sub" not in text
    assert markdown.LOGO_URL not in text


def test_a_clean_review_is_still_signed():
    """``_review_info`` can be empty; the document must not end on a bare rule."""
    text = markdown.render(ReviewResult())
    assert "Reviewed by <b>roborak</b>" in text
    assert not text.rstrip().endswith("---")


# --- the pre-merge check ---------------------------------------------------


@pytest.mark.parametrize("form", list(markdown.Form))
def test_every_review_ends_with_a_pre_merge_check(form):
    """Both surfaces, so the terminal and the merge request state the same thing."""
    result = make_result()
    result.block_on = Severity.CRITICAL
    document = markdown.render(result, form=form)

    assert "Pre-merge check: blocked" in document
    assert "1 finding at or above critical." in document
    assert "🔴 Critical 1" in document


def test_a_clean_review_states_a_pass_rather_than_saying_nothing():
    """An absent section would teach the reader that nothing was checked."""
    document = markdown.render(ReviewResult(block_on=Severity.CRITICAL))
    assert "Pre-merge check: pass" in document
    assert "No findings at or above critical." in document


def test_the_block_names_the_floor_and_where_it_came_from():
    result = make_result()
    result.block_on = Severity.MAJOR
    result.block_on_explicit = True
    document = markdown.render(result)

    assert "Judged against **major** and above, from `--fail-on`" in document

    result.block_on_explicit = False
    assert "from `review.block_on`" in markdown.render(result)


def test_an_implicit_floor_says_the_exit_code_is_not_gated_on_it():
    result = make_result()
    result.block_on = Severity.CRITICAL
    assert "Not gated: pass `--fail-on critical`" in markdown.render(result)


def test_an_explicit_floor_does_not_repeat_the_advice():
    result = make_result()
    result.block_on = Severity.CRITICAL
    result.block_on_explicit = True
    assert "Not gated" not in markdown.render(result)


def test_the_check_is_the_last_section_before_the_footer():
    """It is the one thing a reader who skims the review still has to see."""
    result = make_result()
    result.block_on = Severity.CRITICAL
    document = markdown.render(result)
    signature = markdown._signature(form=markdown.Form.PUBLISHED)
    assert document.index("Pre-merge check") < document.rindex("\n---\n")
    assert document.index("Pre-merge check") < document.index(signature)


def test_an_incomplete_review_is_inconclusive_rather_than_blocked():
    result = make_result()
    result.block_on = Severity.CRITICAL
    result.errors = ["the model timed out"]
    document = markdown.render(result)
    assert "Pre-merge check: inconclusive" in document
    assert "Review did not complete" in document


def test_the_published_check_is_a_callout_the_forge_renders():
    blocked = make_result()
    blocked.block_on = Severity.CRITICAL
    assert "> [!CAUTION]" in markdown.render(blocked)
    assert "> [!TIP]" in markdown.render(ReviewResult(block_on=Severity.CRITICAL))


def test_the_terminal_check_carries_no_html_that_rich_would_drop():
    """``rich.Markdown`` drops HTML silently, taking the verdict with it."""
    result = make_result()
    result.block_on = Severity.CRITICAL
    section = markdown._pre_merge_check(result, form=markdown.Form.TERMINAL)
    assert "<" not in section
    assert "> [!" not in section


def test_the_summary_comment_carries_the_verdict_on_every_run():
    """The comment *is* the report, which is what makes re-runs carry it too."""
    from roborak.publish.base import summary_markdown

    result = make_result()
    result.block_on = Severity.CRITICAL
    assert "Pre-merge check: blocked" in summary_markdown(result)


def test_the_json_verdict_matches_the_rendered_one():
    result = make_result()
    result.block_on = Severity.MINOR
    payload = json.loads(json_out.render(result))
    assert payload["summary"]["verdict"] == Verdict.BLOCKED.value
    assert payload["summary"]["block_on"] == "minor"


def test_describe_states_no_verdict_because_it_judged_nothing():
    """`roborak describe` renders through the same document but never reviews."""
    result = ReviewResult(walkthrough=Walkthrough(title="Add a cache", overview="Adds one."))
    assert result.block_on is None
    document = markdown.render(result)

    assert "Pre-merge check" not in document
    assert "verdict" not in json.loads(json_out.render(result))["summary"]
