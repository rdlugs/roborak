"""Shared publishing concerns.

The one thing every publisher does identically: turn a ``Finding`` into markdown
carrying a committable suggestion block, in the syntax the forge understands.
"""

from __future__ import annotations

import re
from typing import Protocol

from roborak.core.buckets import SUMMARY_BUCKETS, Bucket, group
from roborak.core.models import Finding, ReviewResult
from roborak.core.severity import Kind
from roborak.render import markdown
from roborak.sources.forge import ForgeClient, Target


class Publisher(Protocol):
    def publish(self, result: ReviewResult) -> PublishReport: ...


class PublishReport:
    """What actually happened, so the CLI can report it honestly."""

    def __init__(self) -> None:
        self.posted: list[Finding] = []
        self.skipped_duplicate: list[Finding] = []
        self.failed: list[tuple[Finding, str]] = []
        self.summarised: list[Finding] = []
        """Findings the summary carries instead of the diff: nitpicks, anything
        that could not be anchored, and requirement gaps, which have no honest
        line to point at in the first place."""

        self.summary_posted = False

    @property
    def total_attempted(self) -> int:
        return (
            len(self.posted) + len(self.skipped_duplicate) + len(self.failed) + len(self.summarised)
        )


def finding_markdown(finding: Finding, *, suggestion_syntax: str = "suggestion") -> str:
    """One inline review comment. Rendered by the same code as the report."""
    return markdown.finding_markdown(finding, suggestion_syntax=suggestion_syntax)


def requirement_gaps(result: ReviewResult) -> list[Finding]:
    """Findings that report unmet requirements rather than defects in a line."""
    return [f for f in result.sorted_findings() if f.kind is Kind.REQUIREMENT_GAP]


def inline_findings(result: ReviewResult) -> list[Finding]:
    """The findings that earn an inline thread on the diff.

    Everything else -- nitpicks, unanchorable findings, requirement gaps -- is
    carried by the summary instead. See ``roborak.core.buckets`` for why.
    """
    return group(result).get(Bucket.ACTIONABLE, [])


def summarised_findings(result: ReviewResult) -> list[Finding]:
    """What the summary comment carries because no inline thread could hold it."""
    grouped = group(result)
    return [f for bucket in SUMMARY_BUCKETS for f in grouped.get(bucket, [])]


def summary_markdown(result: ReviewResult) -> str:
    """The comment body: the whole report, exactly as it was printed.

    It repeats the findings that also went out as inline threads, and that is the
    trade being made deliberately. A comment that omitted them would be a fourth
    thing nobody had read before it was published, and the point of this shape is
    that what you saw on screen is what lands on the merge request.
    """
    return markdown.render(result)


_MARKER_RE = re.compile(r"<!--\s*roborak:v[12]:([0-9a-f]{16})\s*-->")


def fingerprints_in(text: str) -> set[str]:
    return set(_MARKER_RE.findall(text))


def remote_fingerprints(target: Target, token: str) -> frozenset[str]:
    """Read identities already published, making remote state authoritative."""
    bodies: list[str] = []
    with ForgeClient(target, token) as client:
        if target.provider == "github":
            root = f"/repos/{target.project}"
            payloads = [
                *client.paginate(f"{root}/issues/{target.number}/comments"),
                *client.paginate(f"{root}/pulls/{target.number}/comments"),
                *client.paginate(f"{root}/pulls/{target.number}/reviews"),
            ]
            bodies.extend(
                str(item.get("body") or "") for item in payloads if isinstance(item, dict)
            )
        else:
            base = f"/projects/{target.encoded_project}/merge_requests/{target.number}"
            notes = client.paginate(f"{base}/notes")
            discussions = client.paginate(f"{base}/discussions")
            bodies.extend(str(item.get("body") or "") for item in notes if isinstance(item, dict))
            for discussion in discussions:
                if not isinstance(discussion, dict):
                    continue
                bodies.extend(
                    str(note.get("body") or "")
                    for note in discussion.get("notes") or []
                    if isinstance(note, dict)
                )
    return frozenset(identity for body in bodies for identity in fingerprints_in(body))
