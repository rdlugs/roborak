"""The configurable pre-merge checks.

The checks decide whether a change is ready to merge without saying anything
about the code, so the tests that matter most are the ones pinning what each
enforcement level is allowed to do: ``off`` must leave no trace, ``warning`` must
report without blocking, and ``error`` must reach the verdict and stop there.
"""

from __future__ import annotations

import pytest

from roborak.core.config import CheckConfig, DocstringCoverageConfig, PreMergeConfig
from roborak.core.models import (
    ChangedFile,
    ChangeSet,
    CheckId,
    CheckOutcome,
    Hunk,
    Issue,
)
from roborak.core.severity import Enforcement
from roborak.llm.client import LLMError
from roborak.premerge.runner import run_checks

PYTHON = '''def documented(value):
    """It says what it does."""
    return value


def bare(value):
    return value
'''

GO = """package main

// Documented explains itself.
func Documented() int {
\treturn 1
}

func Bare() int {
\treturn 2
}
"""


def changed(path: str, language: str | None, content: str | None, start: int, end: int):
    """A file with one hunk covering ``start``-``end``, as a real diff would give."""
    return ChangedFile(
        path=path,
        language=language,
        new_content=content,
        hunks=[
            Hunk(
                old_start=start,
                old_lines=1,
                new_start=start,
                new_lines=end - start + 1,
                content="",
            )
        ],
    )


def changeset(*files, **kwargs) -> ChangeSet:
    kwargs.setdefault("origin", "github")
    kwargs.setdefault("title", "Add a session lookup cache")
    kwargs.setdefault("description", "x" * 80)
    return ChangeSet(files=list(files), **kwargs)


def config(**levels) -> PreMergeConfig:
    """Everything off but the checks named, so one test exercises one check."""
    off = CheckConfig(level=Enforcement.OFF)
    return PreMergeConfig(
        docstring_coverage=levels.get(
            "docstring_coverage", DocstringCoverageConfig(level=Enforcement.OFF)
        ),
        title=levels.get("title", off),
        description=levels.get("description", off),
        linked_issue=levels.get("linked_issue", off),
    )


def only(report, check: CheckId):
    return next(result for result in report.results if result.check is check)


# --- docstring coverage ------------------------------------------------------


def test_a_documented_symbol_the_diff_touched_counts_as_covered():
    report = run_checks(
        changeset(changed("a.py", "python", PYTHON, 2, 2)),
        config(docstring_coverage=DocstringCoverageConfig(level=Enforcement.WARNING)),
    )
    result = only(report, CheckId.DOCSTRING_COVERAGE)
    assert result.outcome is CheckOutcome.PASSED
    assert result.measured == 1.0


def test_an_undocumented_symbol_the_diff_touched_fails_the_threshold():
    report = run_checks(
        changeset(changed("a.py", "python", PYTHON, 6, 6)),
        config(docstring_coverage=DocstringCoverageConfig(level=Enforcement.WARNING)),
    )
    result = only(report, CheckId.DOCSTRING_COVERAGE)
    assert result.outcome is CheckOutcome.FAILED
    assert result.measured == 0.0
    assert "bare" in result.detail


RUST = """/// Documented explains itself.
fn documented() -> i32 {
    1
}

fn bare() -> i32 {
    2
}
"""

JAVASCRIPT = """/** Documented explains itself. */
function documented() {
  return 1;
}

function bare() {
  return 2;
}
"""


@pytest.mark.parametrize(
    ("language", "source", "documented_line", "bare_line"),
    [
        ("go", GO, 5, 9),
        ("rust", RUST, 3, 7),
        ("javascript", JAVASCRIPT, 3, 7),
    ],
)
def test_a_preceding_comment_documents_a_symbol_where_docstrings_do_not_exist(
    language, source, documented_line, bare_line
):
    """Grammars disagree about where a line comment ends, and all of them count."""
    covered = run_checks(
        changeset(changed(f"a.{language}", language, source, documented_line, documented_line)),
        config(docstring_coverage=DocstringCoverageConfig(level=Enforcement.WARNING)),
    )
    uncovered = run_checks(
        changeset(changed(f"b.{language}", language, source, bare_line, bare_line)),
        config(docstring_coverage=DocstringCoverageConfig(level=Enforcement.WARNING)),
    )
    assert only(covered, CheckId.DOCSTRING_COVERAGE).measured == 1.0
    assert only(uncovered, CheckId.DOCSTRING_COVERAGE).measured == 0.0


def test_a_comment_a_blank_line_above_documents_nothing():
    """It is a comment about whatever came before, not about this symbol."""
    source = "package main\n\n// Not about this one.\n\nfunc bare() int {\n\treturn 2\n}\n"
    report = run_checks(
        changeset(changed("a.go", "go", source, 6, 6)),
        config(docstring_coverage=DocstringCoverageConfig(level=Enforcement.WARNING)),
    )
    assert only(report, CheckId.DOCSTRING_COVERAGE).measured == 0.0


def test_a_file_with_no_grammar_is_excluded_rather_than_counted_as_undocumented():
    """We did not look, so we must not report it as a miss."""
    report = run_checks(
        changeset(
            changed("a.py", "python", PYTHON, 2, 2),
            changed("notes.bin", None, "whatever", 1, 1),
        ),
        config(docstring_coverage=DocstringCoverageConfig(level=Enforcement.WARNING)),
    )
    result = only(report, CheckId.DOCSTRING_COVERAGE)
    assert result.outcome is CheckOutcome.PASSED
    assert result.measured == 1.0
    assert "notes.bin" in result.detail


def test_nothing_to_measure_is_not_applicable_rather_than_a_hollow_pass():
    report = run_checks(
        changeset(changed("notes.bin", None, "whatever", 1, 1)),
        config(docstring_coverage=DocstringCoverageConfig(level=Enforcement.WARNING)),
    )
    result = only(report, CheckId.DOCSTRING_COVERAGE)
    assert result.outcome is CheckOutcome.NOT_APPLICABLE
    assert result.measured is None


@pytest.mark.parametrize(
    ("threshold", "expected"),
    [(0.5, CheckOutcome.PASSED), (0.5001, CheckOutcome.FAILED)],
)
def test_the_threshold_boundary_is_inclusive(threshold, expected):
    """Exactly meeting the threshold passes; a project asking for more does not."""
    report = run_checks(
        changeset(
            changed("a.py", "python", PYTHON, 2, 2),
            changed("b.py", "python", PYTHON, 6, 6),
        ),
        config(
            docstring_coverage=DocstringCoverageConfig(
                level=Enforcement.WARNING, threshold=threshold
            )
        ),
    )
    assert only(report, CheckId.DOCSTRING_COVERAGE).outcome is expected


# --- title -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Add a session lookup cache", CheckOutcome.PASSED),
        ("", CheckOutcome.FAILED),
        (None, CheckOutcome.FAILED),
        ("wip", CheckOutcome.FAILED),
        ("fix stuff", CheckOutcome.FAILED),
        ("feat:", CheckOutcome.FAILED),
        ("feat: add a session cache", CheckOutcome.PASSED),
        ("update fix changes", CheckOutcome.FAILED),
    ],
)
def test_the_title_gate(title, expected):
    report = run_checks(changeset(title=title), config(title=CheckConfig()))
    assert only(report, CheckId.TITLE).outcome is expected


# --- description -------------------------------------------------------------


@pytest.mark.parametrize(
    ("description", "expected"),
    [
        ("x" * 80, CheckOutcome.PASSED),
        ("", CheckOutcome.FAILED),
        (None, CheckOutcome.FAILED),
        ("Too short.", CheckOutcome.FAILED),
    ],
)
def test_the_description_gate(description, expected):
    report = run_checks(changeset(description=description), config(description=CheckConfig()))
    assert only(report, CheckId.DESCRIPTION).outcome is expected


def test_an_unfilled_template_is_not_a_description():
    """Headings and empty checkboxes are the template speaking, not the author."""
    template = (
        "## Summary\n\n<!-- say what changed -->\n\n## Checklist\n\n- [ ] Tests\n- [ ] Docs\n"
    )
    report = run_checks(changeset(description=template), config(description=CheckConfig()))
    result = only(report, CheckId.DESCRIPTION)
    assert result.outcome is CheckOutcome.FAILED
    assert "template" in result.summary


@pytest.mark.parametrize("origin", ["local", "paths"])
def test_a_local_diff_has_no_description_to_check(origin):
    """Failing a run for a field the source cannot produce would fail every one."""
    report = run_checks(
        changeset(origin=origin, description=None), config(description=CheckConfig())
    )
    assert only(report, CheckId.DESCRIPTION).outcome is CheckOutcome.NOT_APPLICABLE


# --- linked issue ------------------------------------------------------------


@pytest.mark.parametrize(
    ("description", "expected"),
    [
        ("Closes #12", CheckOutcome.PASSED),
        ("fixes #12", CheckOutcome.PASSED),
        ("Resolves: #12", CheckOutcome.PASSED),
        ("See https://github.com/o/r/issues/12", CheckOutcome.PASSED),
        ("https://gitlab.com/o/r/-/issues/12", CheckOutcome.PASSED),
        ("No reference at all.", CheckOutcome.FAILED),
        ("```\nCloses #12\n```", CheckOutcome.FAILED),
    ],
)
def test_the_linked_issue_gate(description, expected):
    report = run_checks(changeset(description=description), config(linked_issue=CheckConfig()))
    assert only(report, CheckId.LINKED_ISSUE).outcome is expected


def test_an_explicitly_named_issue_links_the_change_without_a_body_mention():
    issue = Issue(
        provider="github", host="github.com", project="o/r", number=66, title="Do a thing"
    )
    report = run_checks(
        changeset(description="No reference at all."),
        config(linked_issue=CheckConfig()),
        issue=issue,
    )
    result = only(report, CheckId.LINKED_ISSUE)
    assert result.outcome is CheckOutcome.PASSED
    assert "#66" in result.summary


def test_a_local_diff_has_no_body_to_carry_an_issue_link():
    report = run_checks(
        changeset(origin="local", description=None), config(linked_issue=CheckConfig())
    )
    assert only(report, CheckId.LINKED_ISSUE).outcome is CheckOutcome.NOT_APPLICABLE


# --- enforcement levels ------------------------------------------------------


def test_an_off_check_leaves_no_row_at_all():
    """Not a skipped row: "nobody asked" is not a result worth a line."""
    report = run_checks(changeset(title=""), config())
    assert report.results == []


def test_a_failed_warning_check_is_reported_and_never_blocks():
    report = run_checks(changeset(title="wip"), config(title=CheckConfig()))
    assert only(report, CheckId.TITLE).outcome is CheckOutcome.FAILED
    assert report.blocking == []
    assert [check.check for check in report.warnings] == [CheckId.TITLE]


def test_a_failed_error_check_blocks():
    report = run_checks(changeset(title="wip"), config(title=CheckConfig(level=Enforcement.ERROR)))
    assert [check.check for check in report.blocking] == [CheckId.TITLE]


def test_a_passing_error_check_blocks_nothing():
    report = run_checks(changeset(), config(title=CheckConfig(level=Enforcement.ERROR)))
    assert report.blocking == []


# --- the model's opinion -----------------------------------------------------


def replies(text: str):
    """A ``complete`` that answers once with ``text``."""

    def complete(system: str, user: str) -> str:
        return text

    return complete


def raises(exc: Exception):
    def complete(system: str, user: str) -> str:
        raise exc

    return complete


ERROR_TITLE = {"title": CheckConfig(level=Enforcement.ERROR)}


def test_no_model_is_asked_until_a_check_is_set_to_error():
    """An advisory note is not worth a call on every review, for every user."""
    asked = []

    def complete(system: str, user: str) -> str:
        asked.append(user)
        return "title_ok: true"

    run_checks(changeset(), config(title=CheckConfig()), complete=complete)
    assert asked == []

    run_checks(changeset(), config(**ERROR_TITLE), complete=complete)
    assert len(asked) == 1


def test_the_opinion_can_fail_a_check_that_passed_its_gate_but_never_blocks():
    """A model's own judgement is not evidence, so it cannot stop a merge."""
    report = run_checks(
        changeset(),
        config(**ERROR_TITLE),
        complete=replies('title_ok: false\ntitle_note: "Names the file, not the change."'),
    )
    result = only(report, CheckId.TITLE)
    assert result.outcome is CheckOutcome.FAILED
    assert result.advisory is True
    assert report.blocking == []
    assert "Names the file" in result.summary


def test_the_opinion_never_overturns_a_deterministic_failure():
    report = run_checks(
        changeset(title="wip"),
        config(**ERROR_TITLE),
        complete=replies("title_ok: true"),
    )
    result = only(report, CheckId.TITLE)
    assert result.outcome is CheckOutcome.FAILED
    assert result.advisory is False
    assert [check.check for check in report.blocking] == [CheckId.TITLE]


def test_a_provider_failure_leaves_the_deterministic_result_and_says_so():
    """ "We could not tell" is never recorded as "we checked"."""
    report = run_checks(
        changeset(),
        config(**ERROR_TITLE),
        complete=raises(LLMError("provider said no")),
    )
    assert only(report, CheckId.TITLE).outcome is CheckOutcome.PASSED
    assert any("unavailable" in note for note in report.notes)


def test_an_unusable_reply_is_no_opinion_rather_than_a_verdict():
    report = run_checks(changeset(), config(**ERROR_TITLE), complete=replies("nonsense: yes"))
    assert only(report, CheckId.TITLE).outcome is CheckOutcome.PASSED
    assert report.notes


def test_the_deterministic_gates_still_run_with_no_model_at_all():
    """``--no-llm`` must not cost a project the policy it gates merges on."""
    report = run_checks(
        changeset(title="wip"),
        PreMergeConfig(title=CheckConfig(level=Enforcement.ERROR)),
        complete=None,
    )
    assert {result.check for result in report.results} == set(CheckId)
    assert [check.check for check in report.blocking] == [CheckId.TITLE]


def test_a_check_that_raises_is_a_note_rather_than_the_end_of_the_review(monkeypatch):
    monkeypatch.setattr(
        "roborak.premerge.runner.check_title",
        lambda *args: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    report = run_checks(changeset(), config(title=CheckConfig(), linked_issue=CheckConfig()))
    assert [result.check for result in report.results] == [CheckId.LINKED_ISSUE]
    assert any("boom" in note for note in report.notes)
