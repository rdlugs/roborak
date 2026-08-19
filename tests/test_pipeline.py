"""End-to-end pipeline tests with a stubbed model.

Replaying a canned reply exercises every stage after the LLM -- parsing,
anchoring, dedupe, severity filtering -- without spending anything.
"""

from __future__ import annotations

import textwrap
from dataclasses import dataclass
from pathlib import Path

import pytest

from roborak.analysis.reviewer import Reviewer
from roborak.context.diff import parse_diff
from roborak.core.config import Config
from roborak.core.models import ChangeSet, Finding
from roborak.core.severity import Category, Kind, Severity
from roborak.llm.client import LLMResponse

DIFF = textwrap.dedent(
    """\
    diff --git a/app/auth.py b/app/auth.py
    --- a/app/auth.py
    +++ b/app/auth.py
    @@ -8,6 +8,9 @@ def get_session(request):
         user_id = request.args.get("user_id")
         if not user_id:
             return None
    +    row = db.execute("SELECT * FROM sessions WHERE user = " + user_id)
    +    if row.token == request.args.get("token"):
    +        return row
         return None
    """
)


@dataclass
class StubLLM:
    """Stands in for ``LLMClient``; records the prompt it was handed."""

    reply: str
    system: str = ""
    user: str = ""
    context_budget: int = 100_000

    def complete(self, system: str, user: str) -> LLMResponse:
        self.system, self.user = system, user
        return LLMResponse(text=self.reply, model="stub")

    def count_tokens(self, text: str) -> int:
        return len(text) // 4


def make_changeset() -> ChangeSet:
    return ChangeSet(files=parse_diff(DIFF), origin="local", title="Add session lookup")


GOOD_REPLY = textwrap.dedent(
    """\
    findings:
      - file: app/auth.py
        start_line: 11
        end_line: 11
        severity: critical
        category: security
        kind: potential_issue
        effort: quick_win
        title: SQL injection via user_id
        body: >
          user_id comes straight from the query string and is concatenated into SQL,
          so any caller can rewrite the statement. Use a bound parameter.
        confidence: 0.95
        suggestion: |
          row = db.execute("SELECT * FROM sessions WHERE user = ?", (user_id,))
      - file: app/auth.py
        start_line: 12
        end_line: 12
        severity: major
        category: security
        kind: potential_issue
        effort: quick_win
        title: Timing-unsafe token comparison
        body: Comparing tokens with == leaks length and content through timing.
        confidence: 0.8
    """
)


def review_with(reply: str, config: Config | None = None, tmp: Path | None = None):
    llm = StubLLM(reply=reply)
    reviewer = Reviewer(config=config or Config(), repo=tmp or Path("/nonexistent"), llm=llm)
    return reviewer.review(make_changeset()), llm


def test_end_to_end_produces_anchored_findings(tmp_path):
    result, _ = review_with(GOOD_REPLY, tmp=tmp_path)
    assert len(result.findings) == 2
    top = result.findings[0]
    assert top.severity is Severity.CRITICAL
    assert top.category is Category.SECURITY
    assert top.kind is Kind.POTENTIAL_ISSUE
    assert top.file == "app/auth.py"
    assert top.start_line == 11
    assert "?" in (top.suggestion or "")
    assert result.counts_by_severity[Severity.CRITICAL] == 1
    assert result.has_blocking


def test_findings_are_sorted_by_severity(tmp_path):
    result, _ = review_with(GOOD_REPLY, tmp=tmp_path)
    ranks = [f.severity.rank for f in result.findings]
    assert ranks == sorted(ranks, reverse=True)


def test_prompt_carries_true_line_numbers(tmp_path):
    _, llm = review_with(GOOD_REPLY, tmp=tmp_path)
    assert "11 +    row = db.execute" in llm.user
    assert "app/auth.py" in llm.user
    assert "Add session lookup" in llm.user


def test_finding_outside_the_diff_is_dropped(tmp_path):
    reply = textwrap.dedent(
        """\
        findings:
          - file: app/auth.py
            start_line: 400
            end_line: 400
            severity: critical
            category: bug
            body: Something far away from anything this change touched.
        """
    )
    result, _ = review_with(reply, tmp=tmp_path)
    assert result.findings == []


def test_near_miss_anchor_is_snapped_onto_a_changed_line(tmp_path):
    reply = textwrap.dedent(
        """\
        findings:
          - file: app/auth.py
            start_line: 10
            end_line: 10
            severity: major
            category: security
            body: The query just below builds SQL by concatenation.
        """
    )
    result, _ = review_with(reply, tmp=tmp_path)
    assert len(result.findings) == 1
    assert result.findings[0].start_line == 11


def test_hallucinated_file_is_dropped(tmp_path):
    reply = textwrap.dedent(
        """\
        findings:
          - file: app/does_not_exist.py
            start_line: 1
            end_line: 1
            severity: critical
            category: bug
            body: A file that is not in this diff at all.
        """
    )
    result, _ = review_with(reply, tmp=tmp_path)
    assert result.findings == []


def test_severity_floor_filters(tmp_path):
    config = Config()
    config.review.severity_floor = Severity.CRITICAL
    result, _ = review_with(GOOD_REPLY, config, tmp=tmp_path)
    assert [f.severity for f in result.findings] == [Severity.CRITICAL]


def test_low_confidence_is_filtered(tmp_path):
    reply = textwrap.dedent(
        """\
        findings:
          - file: app/auth.py
            start_line: 11
            end_line: 11
            severity: major
            category: bug
            confidence: 0.2
            body: A guess the model was not sure about.
        """
    )
    result, _ = review_with(reply, tmp=tmp_path)
    assert result.findings == []


def test_empty_findings_reply(tmp_path):
    result, _ = review_with("findings: []", tmp=tmp_path)
    assert result.findings == []
    assert not result.errors


def test_fenced_reply_is_tolerated(tmp_path):
    result, _ = review_with(f"```yaml\n{GOOD_REPLY}```", tmp=tmp_path)
    assert len(result.findings) == 2


def test_truncated_reply_keeps_whole_findings(tmp_path):
    # Simulate the model hitting its token ceiling partway through finding two.
    truncated = GOOD_REPLY[: GOOD_REPLY.index("Timing-unsafe") + 6]
    result, _ = review_with(truncated, tmp=tmp_path)
    # The first finding is complete and must survive the second being cut off.
    assert len(result.findings) >= 1
    assert result.findings[0].title == "SQL injection via user_id"


def test_ignored_paths_are_never_reviewed(tmp_path):
    lock_diff = textwrap.dedent(
        """\
        diff --git a/uv.lock b/uv.lock
        --- a/uv.lock
        +++ b/uv.lock
        @@ -1,1 +1,2 @@
         a
        +b
        """
    )
    changeset = ChangeSet(files=parse_diff(lock_diff))
    llm = StubLLM(reply=GOOD_REPLY)
    result = Reviewer(config=Config(), repo=tmp_path, llm=llm).review(changeset)
    assert result.findings == []
    assert llm.user == "", "the model should not have been called at all"


def test_static_findings_reach_the_prompt(tmp_path):
    static = [
        Finding(
            file="app/auth.py",
            start_line=11,
            end_line=11,
            severity=Severity.MAJOR,
            category=Category.SECURITY,
            title="S608 hardcoded SQL",
            body="Possible SQL injection.",
            source="static",
            tool="ruff",
        )
    ]
    llm = StubLLM(reply="findings: []")
    reviewer = Reviewer(config=Config(), repo=tmp_path, llm=llm, static_findings=static)
    result = reviewer.review(make_changeset())
    assert "[ruff] S608 hardcoded SQL" in llm.user
    # A static finding still stands on its own when the model drops it.
    assert len(result.findings) == 1
    assert result.findings[0].source == "static"


def test_llm_explanation_wins_over_duplicate_static_finding(tmp_path):
    static = [
        Finding(
            file="app/auth.py",
            start_line=11,
            end_line=11,
            severity=Severity.MAJOR,
            category=Category.SECURITY,
            title="S608",
            body="Possible SQL injection.",
            source="static",
            tool="ruff",
        )
    ]
    llm = StubLLM(reply=GOOD_REPLY)
    reviewer = Reviewer(config=Config(), repo=tmp_path, llm=llm, static_findings=static)
    result = reviewer.review(make_changeset())
    on_line_11 = [f for f in result.findings if f.start_line == 11]
    assert len(on_line_11) == 1
    assert on_line_11[0].source == "llm"
    assert on_line_11[0].severity is Severity.CRITICAL


def test_llm_failure_is_reported_not_swallowed(tmp_path):
    class Failing(StubLLM):
        def complete(self, system: str, user: str):
            from roborak.llm.client import LLMError

            raise LLMError("provider exploded")

    reviewer = Reviewer(config=Config(), repo=tmp_path, llm=Failing(reply=""))
    result = reviewer.review(make_changeset())
    assert result.findings == []
    assert result.errors and "provider exploded" in result.errors[0]


def test_empty_changeset_short_circuits(tmp_path):
    llm = StubLLM(reply=GOOD_REPLY)
    result = Reviewer(config=Config(), repo=tmp_path, llm=llm).review(ChangeSet())
    assert result.findings == []
    assert llm.user == ""


@pytest.mark.parametrize(
    ("value", "expected"),
    [("0.9", 0.9), (85, 0.85), ("nonsense", 0.8), (2.5, 0.025), (-1, 0.0)],
)
def test_confidence_coercion(value, expected):
    from roborak.llm.parser import _as_confidence

    assert _as_confidence(value) == pytest.approx(expected)
