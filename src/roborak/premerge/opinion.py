"""What a model thinks of a title, a description and a linked issue.

Strictly capped. An opinion may turn a deterministic pass into a reported
failure, and the result is marked ``advisory`` so it never reaches the verdict.
This is the check-stage form of the rule the evidence policy already applies to
findings: a model's own confidence is not grounds to fail a build.

Nothing here can rescue a deterministic failure either. The gates in ``text.py``
answer questions with right answers; this one answers a question of judgement,
and judgement does not overturn a fact.
"""

from __future__ import annotations

from collections.abc import Callable

from roborak.core.models import (
    ChangeSet,
    CheckId,
    CheckOutcome,
    CheckResult,
    Issue,
    Walkthrough,
)
from roborak.llm.client import LLMError
from roborak.llm.parser import ParseError, parse_premerge_opinion
from roborak.llm.prompt import build_premerge_prompt

Complete = Callable[[str, str], str]

_ASKABLE = (CheckId.TITLE, CheckId.DESCRIPTION, CheckId.LINKED_ISSUE)


def apply_opinion(
    results: list[CheckResult],
    changeset: ChangeSet,
    issue: Issue | None,
    walkthrough: Walkthrough | None,
    complete: Complete,
) -> str | None:
    """Layer a model's opinion onto results that already passed their gates.

    Returns a note when the opinion could not be had, so the report can say the
    layer was unavailable rather than leave a reader to infer it from silence.
    Mutates ``results`` in place: they are this stage's own objects.
    """
    candidates = {
        result.check: result
        for result in results
        if result.check in _ASKABLE and result.outcome is CheckOutcome.PASSED
    }
    if not candidates:
        return None

    if not _summary(walkthrough).strip():
        return "The pre-merge quality opinion was unavailable: no walkthrough summary."

    prompt = build_premerge_prompt(
        title=changeset.title or "",
        description=changeset.description or "",
        issue_title=issue.title if issue else "",
        issue_body=issue.body if issue else "",
        changed_files=[file.path for file in changeset.files],
        walkthrough_summary=_summary(walkthrough),
    )
    try:
        reply = complete(prompt.system, prompt.user)
        opinion = parse_premerge_opinion(reply)
    except (LLMError, ParseError) as exc:
        return f"The pre-merge quality opinion was unavailable: {exc}"
    verdicts = (
        (CheckId.TITLE, opinion.title, opinion.title_note),
        (CheckId.DESCRIPTION, opinion.description, opinion.description_note),
        (CheckId.LINKED_ISSUE, opinion.linked_issue, opinion.linked_issue_note),
    )
    if all(verdict is None for _, verdict, _ in verdicts):
        return "The model returned no usable pre-merge quality opinion."

    for check, verdict, note in verdicts:
        result = candidates.get(check)
        if result is None or verdict is None:
            continue
        result.opinion = note
        if verdict is False:
            result.outcome = CheckOutcome.FAILED
            result.advisory = True
            result.summary = note or f"The model judged the {check.value} inadequate."
            result.detail = _advisory_detail(result.detail)
    return None


def _advisory_detail(existing: str) -> str:
    explanation = (
        "Reported by the model alone, so it does not block: an opinion is not "
        "evidence, and the deterministic gate for this check passed."
    )
    return f"{existing}\n\n{explanation}" if existing else explanation


def _summary(walkthrough: Walkthrough | None) -> str:
    return walkthrough.overview if walkthrough is not None else ""
