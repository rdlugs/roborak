"""Parser tests.

A model reply is untrusted input. The contract is: never raise on junk, never let
one bad entry discard a good one, and never invent a finding out of nothing.
"""

from __future__ import annotations

import textwrap

import pytest

from roborak.core.severity import Category, Effort, Evidence, Kind, Severity
from roborak.llm.parser import (
    ParseError,
    parse_findings,
    parse_requirement_evidence,
    parse_walkthrough,
    strip_fences,
)


def test_plain_yaml():
    findings = parse_findings(
        "findings:\n"
        "  - file: a.py\n"
        "    start_line: 3\n"
        "    end_line: 5\n"
        "    severity: critical\n"
        "    category: security\n"
        "    body: Injection.\n"
    )
    assert len(findings) == 1
    assert findings[0].severity is Severity.CRITICAL
    assert findings[0].end_line == 5


@pytest.mark.parametrize(
    "wrapper",
    [
        "```yaml\n{body}\n```",
        "```yml\n{body}\n```",
        "```\n{body}\n```",
        "```yaml\n{body}",
    ],
)
def test_fenced_replies(wrapper):
    body = "findings:\n  - file: a.py\n    start_line: 1\n    body: Bad.\n"
    assert len(parse_findings(wrapper.format(body=body))) == 1


def test_empty_and_null_replies():
    assert parse_findings("findings: []") == []
    assert parse_findings("") == []
    assert parse_findings("findings:") == []


def test_non_mapping_reply_raises():
    with pytest.raises(ParseError):
        parse_findings("- just\n- a\n- list")


def test_findings_must_be_a_list():
    with pytest.raises(ParseError):
        parse_findings("findings: nope")


def test_entry_without_a_file_is_skipped():
    findings = parse_findings(
        "findings:\n"
        "  - start_line: 1\n"
        "    body: No file given.\n"
        "  - file: b.py\n"
        "    start_line: 2\n"
        "    body: This one is fine.\n"
    )
    assert [f.file for f in findings] == ["b.py"]


def test_entry_without_a_body_is_skipped():
    """A title with no explanation is not a review comment worth posting."""
    findings = parse_findings("findings:\n  - file: a.py\n    start_line: 1\n    title: Hmm\n")
    assert findings == []


def test_description_is_accepted_as_a_body_alias():
    findings = parse_findings(
        "findings:\n  - file: a.py\n    start_line: 1\n    description: Explained here.\n"
    )
    assert findings[0].body == "Explained here."


def test_title_falls_back_to_the_first_sentence():
    findings = parse_findings(
        "findings:\n  - file: a.py\n    start_line: 1\n    body: Leaks memory. And more.\n"
    )
    assert findings[0].title == "Leaks memory"


def test_unknown_enum_values_fall_back_rather_than_failing():
    findings = parse_findings(
        "findings:\n"
        "  - file: a.py\n"
        "    start_line: 1\n"
        "    severity: catastrophic\n"
        "    category: vibes\n"
        "    kind: shrug\n"
        "    effort: enormous\n"
        "    body: Something.\n"
    )
    f = findings[0]
    assert f.severity is Severity.MINOR
    assert f.category is Category.MAINTAINABILITY
    assert f.kind is Kind.POTENTIAL_ISSUE
    assert f.effort is Effort.MODERATE


def test_enum_values_are_normalised():
    findings = parse_findings(
        "findings:\n"
        "  - file: a.py\n"
        "    start_line: 1\n"
        "    severity: CRITICAL\n"
        "    kind: Potential Issue\n"
        "    effort: quick-win\n"
        "    body: Something.\n"
    )
    assert findings[0].severity is Severity.CRITICAL
    assert findings[0].kind is Kind.POTENTIAL_ISSUE
    assert findings[0].effort is Effort.QUICK_WIN


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("12", 12), (12, 12), ("line 12", 12), ("L12", 12), ("nonsense", None), (True, None)],
)
def test_line_number_coercion(raw, expected):
    reply = f"findings:\n  - file: a.py\n    start_line: {raw!r}\n    body: X.\n"
    findings = parse_findings(reply)
    if expected is None:
        assert findings == []
    else:
        assert findings[0].start_line == expected


def test_end_line_defaults_to_start_and_never_precedes_it():
    findings = parse_findings(
        "findings:\n"
        "  - file: a.py\n"
        "    start_line: 10\n"
        "    body: X.\n"
        "  - file: a.py\n"
        "    start_line: 10\n"
        "    end_line: 4\n"
        "    body: Y.\n"
    )
    assert findings[0].end_line == 10
    assert findings[1].end_line == 10


def test_valid_files_filter_drops_hallucinated_paths():
    reply = (
        "findings:\n"
        "  - file: real.py\n    start_line: 1\n    body: X.\n"
        "  - file: imaginary.py\n    start_line: 1\n    body: Y.\n"
    )
    findings = parse_findings(reply, valid_files={"real.py"})
    assert [f.file for f in findings] == ["real.py"]


def test_suggestion_is_stripped_of_fences_and_diff_markers():
    reply = textwrap.dedent(
        """\
        findings:
          - file: a.py
            start_line: 1
            body: X.
            suggestion: |
              ```python
              value = compute()
              ```
        """
    )
    assert parse_findings(reply)[0].suggestion == "value = compute()"


def test_suggestion_with_leading_plus_markers_is_cleaned():
    reply = textwrap.dedent(
        """\
        findings:
          - file: a.py
            start_line: 1
            body: X.
            suggestion: |
              +value = compute()
              +return value
        """
    )
    assert parse_findings(reply)[0].suggestion == "value = compute()\nreturn value"


def test_blank_suggestion_becomes_none():
    reply = "findings:\n  - file: a.py\n    start_line: 1\n    body: X.\n    suggestion: '   '\n"
    assert parse_findings(reply)[0].suggestion is None


def test_malformed_yaml_recovers_the_parseable_prefix():
    reply = textwrap.dedent(
        """\
        findings:
          - file: a.py
            start_line: 1
            body: First one, complete.
          - file: b.py
            start_line: 2
            body: "unterminated quote
        """
    )
    findings = parse_findings(reply)
    assert [f.file for f in findings] == ["a.py"]


def test_unrecoverable_yaml_yields_nothing_rather_than_raising():
    assert parse_findings("findings:\n  - [unclosed\n") == []


def test_strip_fences_leaves_plain_text_alone():
    assert strip_fences("findings: []") == "findings: []"


def test_walkthrough_parsing():
    reply = textwrap.dedent(
        """\
        title: Add session lookup
        overview: Introduces a session cache.
        estimated_effort: 3
        labels: [feature, security]
        file_summaries:
          - path: app/auth.py
            summary: Adds get_session.
          - summary: no path, should be dropped
        sequence_diagram: |
          ```mermaid
          sequenceDiagram
            Client->>API: GET /session
          ```
        """
    )
    walkthrough = parse_walkthrough(reply)
    assert walkthrough.title == "Add session lookup"
    assert walkthrough.estimated_effort == 3
    assert walkthrough.labels == ["feature", "security"]
    assert len(walkthrough.file_summaries) == 1
    assert walkthrough.file_summaries[0].path == "app/auth.py"
    assert walkthrough.sequence_diagram is not None
    assert walkthrough.sequence_diagram.startswith("sequenceDiagram")
    assert "```" not in walkthrough.sequence_diagram


def test_walkthrough_parses_a_general_mermaid_flow():
    reply = textwrap.dedent(
        """\
        flow_diagram: |
          flowchart TD
            Boot --> LegacyRoutes
            LegacyRoutes --> TypedRoutes
        """
    )

    walkthrough = parse_walkthrough(reply)

    assert walkthrough.sequence_diagram is not None
    assert walkthrough.sequence_diagram.startswith("flowchart TD")


def test_walkthrough_rejects_out_of_range_effort():
    assert parse_walkthrough("estimated_effort: 9").estimated_effort is None
    assert parse_walkthrough("estimated_effort: 0").estimated_effort is None


def test_walkthrough_of_an_empty_reply():
    walkthrough = parse_walkthrough("")
    assert walkthrough.title is None
    assert walkthrough.file_summaries == []


def test_fingerprint_ignores_line_numbers_but_not_content():
    from roborak.core.models import Finding

    def make(line: int, body: str) -> Finding:
        return Finding(
            file="a.py",
            start_line=line,
            end_line=line,
            severity=Severity.MAJOR,
            category=Category.BUG,
            title="t",
            body=body,
        )

    assert make(10, "Same problem.").fingerprint == make(40, "Same   problem.").fingerprint
    assert make(10, "Same problem.").fingerprint != make(10, "Different one.").fingerprint


def test_requirement_evidence_is_optional_and_tolerates_bad_entries():
    text = """
findings: []
requirement_evidence:
  - requirement: Rate limit requests
    file: api.py
    evidence: Adds a limiter before dispatch.
  - nonsense
  - requirement: Missing explanation
"""
    assert parse_requirement_evidence(text) == [
        {
            "requirement": "Rate limit requests",
            "file": "api.py",
            "evidence": "Adds a limiter before dispatch.",
        }
    ]
    assert parse_requirement_evidence("findings: []\nrequirement_evidence: nope") == []


def _one(**fields: object):
    """Parse a single finding, with the boilerplate fields filled in."""
    entry = {
        "file": "a.py",
        "start_line": 3,
        "end_line": 3,
        "severity": "critical",
        "category": "bug",
        "body": "The divisor can be zero.",
        **fields,
    }
    body = "\n".join(f"    {key}: {value}" for key, value in entry.items())
    findings = parse_findings(f"findings:\n  -\n{body}\n")
    assert len(findings) == 1
    return findings[0]


def test_evidence_is_read_when_the_model_supplies_both_halves():
    finding = _one(
        evidence="execution_path",
        evidence_note="count is 0 for an empty batch, so line 3 raises.",
    )
    assert finding.evidence is Evidence.EXECUTION_PATH
    assert finding.evidence_note == "count is 0 for an empty batch, so line 3 raises."


def test_a_missing_evidence_key_is_unverified_rather_than_assumed():
    finding = _one()
    assert finding.evidence is Evidence.UNVERIFIED
    assert finding.evidence_note == ""


def test_an_unknown_evidence_label_falls_back_to_unverified():
    finding = _one(evidence="vibes", evidence_note="I have a feeling about this one.")
    assert finding.evidence is Evidence.UNVERIFIED


def test_a_proven_label_with_no_note_behind_it_is_not_evidence():
    """Picking the word is not showing the path, and must not buy a blocker slot."""
    finding = _one(evidence="execution_path")
    assert finding.evidence is Evidence.UNVERIFIED
    assert finding.evidence_note == ""


def test_an_honest_unverified_claim_keeps_the_note_it_offered():
    finding = _one(
        evidence="unverified",
        evidence_note="Depends on validate(), which was not shown.",
    )
    assert finding.evidence is Evidence.UNVERIFIED
    assert finding.evidence_note == "Depends on validate(), which was not shown."
