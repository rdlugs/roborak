"""The deterministic half of the title, description and linked-issue checks.

Everything here runs without a model, so ``--no-llm`` still gets a real answer
and CI does not silently lose its merge policy when a provider is down. What a
model adds on top is an opinion, and an opinion is handled in ``opinion.py``
under a much lower ceiling.
"""

from __future__ import annotations

import re

from roborak.core.models import (
    ChangeSet,
    CheckId,
    CheckOutcome,
    CheckResult,
    Issue,
)
from roborak.core.severity import Enforcement

MIN_TITLE_LENGTH = 10
MIN_DESCRIPTION_LENGTH = 50
"""Characters of prose, after the boilerplate a template contributes is removed.
Both are floors on effort, not on quality: quality is what the model is for."""

_PLACEHOLDER_TITLES = frozenset(
    {"wip", "update", "updates", "fix", "fixes", "changes", "untitled", "draft", "test", "temp"}
)

_CONVENTIONAL_PREFIX = re.compile(r"^\w+(\([^)]*\))?!?:\s*")
_ISSUE_REFERENCE = re.compile(
    r"\b(clos(?:e|es|ed)|fix(?:es|ed)?|resolv(?:e|es|ed)|implements|refs?)\b[\s:]*#(\d+)",
    re.IGNORECASE,
)
_ISSUE_URL = re.compile(
    r"https?://[^\s)]+/(?:issues|-/issues)/(\d+)",
    re.IGNORECASE,
)
_FENCE = re.compile(r"```.*?```", re.DOTALL)
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_TEMPLATE_LINE = re.compile(r"^\s*(?:#{1,6}\s.*|[-*+]\s*\[[ xX]\].*)$", re.MULTILINE)
"""Headings and checklist items, removed whole. Their text is the template's
words, not the author's, so keeping the label while dropping the marker would let
an untouched checklist satisfy a floor on how much the author actually wrote."""

_QUOTE_LINE = re.compile(r"^[ \t]*>.*$", re.MULTILINE)
"""Quoted lines, removed whole for the same reason as headings: a template
that states its instructions in a blockquote is still the template speaking,
and stripping only the ``>`` would leave its words counting as the author's."""

_LOCAL_ORIGINS = frozenset({"local", "paths"})


def check_title(changeset: ChangeSet, level: Enforcement) -> CheckResult:
    """Whether the change is named well enough to be found again."""
    # Local titles are commit subjects or display labels, not request titles.
    if changeset.origin in _LOCAL_ORIGINS:
        return CheckResult(
            check=CheckId.TITLE,
            level=level,
            outcome=CheckOutcome.NOT_APPLICABLE,
            summary="This local source has no title for the reviewed change to check.",
        )
    title = (changeset.title or "").strip()
    reason = _title_problem(title)
    if reason is None:
        return CheckResult(
            check=CheckId.TITLE,
            level=level,
            outcome=CheckOutcome.PASSED,
            summary="The title names the change.",
        )
    return CheckResult(
        check=CheckId.TITLE,
        level=level,
        outcome=CheckOutcome.FAILED,
        summary=reason,
        detail=f"Title: `{title}`" if title else "",
    )


def _title_problem(title: str) -> str | None:
    if not title:
        return "The change has no title."
    if len(title) < MIN_TITLE_LENGTH:
        return f"The title is shorter than {MIN_TITLE_LENGTH} characters."
    stem = _CONVENTIONAL_PREFIX.sub("", title).strip()
    if not stem:
        return "The title is a conventional-commit prefix with nothing after it."
    words = [word for word in re.split(r"[\s_/-]+", stem) if word]
    if len(words) < 3:
        return "The title is fewer than three words."
    if stem.lower().strip(".") in _PLACEHOLDER_TITLES:
        return "The title is a placeholder."
    if all(word.lower() in _PLACEHOLDER_TITLES for word in words):
        return "The title is made only of placeholder words."
    return None


def check_description(changeset: ChangeSet, level: Enforcement) -> CheckResult:
    """Whether the change explains itself to whoever reads it next."""
    if changeset.origin in _LOCAL_ORIGINS:
        return CheckResult(
            check=CheckId.DESCRIPTION,
            level=level,
            outcome=CheckOutcome.NOT_APPLICABLE,
            summary="A local diff has no description to check.",
        )
    prose = _prose(changeset.description or "")
    if not prose:
        summary = (
            "The description is only an unfilled template."
            if (changeset.description or "").strip()
            else "The change has no description."
        )
        return CheckResult(
            check=CheckId.DESCRIPTION,
            level=level,
            outcome=CheckOutcome.FAILED,
            summary=summary,
        )
    if len(prose) < MIN_DESCRIPTION_LENGTH:
        return CheckResult(
            check=CheckId.DESCRIPTION,
            level=level,
            outcome=CheckOutcome.FAILED,
            summary=f"The description carries under {MIN_DESCRIPTION_LENGTH} characters of prose.",
            detail=f"{len(prose)} characters once headings, checklists, quotes and comments "
            "are removed.",
        )
    return CheckResult(
        check=CheckId.DESCRIPTION,
        level=level,
        outcome=CheckOutcome.PASSED,
        summary="The description explains the change.",
    )


def _prose(description: str) -> str:
    """What is left once the template stops speaking for the author.

    HTML comments, headings, unticked checkboxes and quoted blocks are all things
    a template contributes on its own, so counting them would let an untouched
    template pass a length floor it never actually met.
    """
    text = _HTML_COMMENT.sub("", description)
    text = _FENCE.sub("", text)
    text = _TEMPLATE_LINE.sub("", text)
    text = _QUOTE_LINE.sub("", text)
    return " ".join(text.split())


def check_linked_issue(
    changeset: ChangeSet, issue: Issue | None, level: Enforcement
) -> CheckResult:
    """Whether this change says which tracked work it belongs to."""
    if issue is not None:
        detail = (
            f"Reviewed against {issue.reference}. Whether the change actually does what the "
            "issue asked is judged as a requirement gap, not here."
        )
        return CheckResult(
            check=CheckId.LINKED_ISSUE,
            level=level,
            outcome=CheckOutcome.PASSED,
            summary=f"Linked to {issue.reference}.",
            detail=detail,
        )
    if changeset.origin in _LOCAL_ORIGINS:
        return CheckResult(
            check=CheckId.LINKED_ISSUE,
            level=level,
            outcome=CheckOutcome.NOT_APPLICABLE,
            summary="A local diff has no request body to carry an issue link.",
        )
    references = _issue_references(changeset)
    if references:
        listed = ", ".join(f"#{number}" for number in references)
        return CheckResult(
            check=CheckId.LINKED_ISSUE,
            level=level,
            outcome=CheckOutcome.PASSED,
            summary=f"Linked to {listed}.",
        )
    return CheckResult(
        check=CheckId.LINKED_ISSUE,
        level=level,
        outcome=CheckOutcome.FAILED,
        summary="No linked issue.",
        detail="Reference one with `Closes #123` in the description, or pass `--issue`.",
    )


def _issue_references(changeset: ChangeSet) -> list[str]:
    """Issue numbers named in the title or description, in order, deduplicated.

    Fenced code is stripped first: a diff that documents the syntax of a closing
    keyword must not thereby satisfy the check it is documenting.
    """
    text = _FENCE.sub("", f"{changeset.title or ''}\n{changeset.description or ''}")
    found = [match.group(2) for match in _ISSUE_REFERENCE.finditer(text)]
    found.extend(match.group(1) for match in _ISSUE_URL.finditer(text))
    return list(dict.fromkeys(found))
