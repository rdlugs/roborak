"""Shared publishing concerns.

The one thing every publisher does identically: turn a ``Finding`` into markdown
carrying a committable suggestion block, in the syntax the forge understands.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from roborak.core.buckets import SUMMARY_BUCKETS, Bucket, group
from roborak.core.models import Finding, ReviewResult
from roborak.core.severity import Kind
from roborak.render import markdown
from roborak.sources.base import SourceError
from roborak.sources.forge import ForgeClient, Target

log = logging.getLogger(__name__)


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
        self.summary_updated = False
        """An overview roborak had already published was edited in place rather
        than a second one appended beside it."""

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


def refreshed_summary_markdown(result: ReviewResult) -> str:
    """The report, prefaced by a line saying the overview was re-narrated.

    Editing a comment is silent -- a reader who saw the old overview has no way
    to tell the new one apart from it. One line naming the commit it now
    describes is what makes the update visible.
    """
    head = (result.changeset.head_sha if result.changeset else "") or ""
    stamp = datetime.now(UTC).strftime("%Y-%m-%d")
    where = f" for `{head[:8]}`" if head else ""
    return f"_Overview refreshed{where} on {stamp}._\n\n{summary_markdown(result)}"


def publish_summary(
    client: ForgeClient,
    result: ReviewResult,
    report: PublishReport,
    *,
    post_path: str,
    ref: SummaryRef | None,
    refreshed: bool,
) -> None:
    """Put the overview where it belongs: over the old one, or beside nothing.

    A failed edit falls back to posting fresh. A duplicated overview is noise; a
    review whose overview silently vanished because someone deleted the comment
    roborak was aiming at is a missing review.
    """
    if ref is not None:
        body = refreshed_summary_markdown(result) if refreshed else summary_markdown(result)
        try:
            send = client.put if ref.method == "PUT" else client.patch
            send(ref.edit_path, {"body": body})
        except SourceError as exc:
            log.warning("could not update the existing summary (%s); posting a new one", exc)
        else:
            report.summary_updated = True
            report.summary_posted = True
            return

    client.post(post_path, {"body": summary_markdown(result)})
    report.summary_posted = True


_MARKER_RE = re.compile(r"<!--\s*roborak:v[12]:([0-9a-f]{16})\s*-->")
_SUMMARY_MARKER = f"<!-- {markdown.REVIEW_MARKER} -->"
_FLOW_RE = re.compile(rf"<!--\s*{markdown.FLOW_MARKER_PREFIX}:([0-9a-f]{{16}})\s*-->")


def fingerprints_in(text: str) -> set[str]:
    return set(_MARKER_RE.findall(text))


@dataclass(frozen=True)
class SummaryRef:
    """An overview roborak has already published, and how to edit it.

    Only a comment written by the publishing token's own account becomes one of
    these -- see ``_offer``. The markers are public, so authorship is the only
    thing separating roborak's overview from a copy of it.
    """

    edit_path: str
    method: str
    """``PUT`` or ``PATCH`` -- each forge surface takes only one of them."""

    flow: str = ""
    """The change shape the published overview narrates, empty if it predates
    the marker. An empty digest never matches, so such a comment is refreshed
    once and carries a digest from then on."""


@dataclass(frozen=True)
class RemoteState:
    """What the merge request already says, so remote stays authoritative."""

    fingerprints: frozenset[str] = frozenset()
    summary: SummaryRef | None = None


def remote_state(target: Target, token: str) -> RemoteState:
    """Read what an earlier run published: inline identities and the overview.

    One pass for both. The payloads that carry the fingerprints are the same ones
    that carry the summary's id, and fetching them twice would double the cost of
    every ``--post`` run.
    """
    bodies: list[str] = []
    candidates: list[SummaryRef] = []

    with ForgeClient(target, token) as client:
        viewer = _viewer(client)
        if target.provider == "github":
            root = f"/repos/{target.project}"
            surfaces = [
                (f"{root}/issues/{target.number}/comments", f"{root}/issues/comments", "PATCH"),
                (f"{root}/pulls/{target.number}/comments", None, ""),
                (
                    f"{root}/pulls/{target.number}/reviews",
                    f"{root}/pulls/{target.number}/reviews",
                    "PUT",
                ),
            ]
            for path, edit_root, method in surfaces:
                for item in client.paginate(path):
                    body = _absorb(item, bodies)
                    if body and edit_root:
                        _offer(candidates, item, edit_root, method, body, viewer)
        else:
            base = f"/projects/{target.encoded_project}/merge_requests/{target.number}"
            for item in client.paginate(f"{base}/notes"):
                if body := _absorb(item, bodies):
                    _offer(candidates, item, f"{base}/notes", "PUT", body, viewer)
            for discussion in client.paginate(f"{base}/discussions"):
                if not isinstance(discussion, dict):
                    continue
                for note in discussion.get("notes") or []:
                    _absorb(note, bodies)

    return RemoteState(
        fingerprints=frozenset(identity for body in bodies for identity in fingerprints_in(body)),
        summary=candidates[-1] if candidates else None,
    )


def remote_fingerprints(target: Target, token: str) -> frozenset[str]:
    """Read identities already published, making remote state authoritative."""
    return remote_state(target, token).fingerprints


def _absorb(item: Any, bodies: list[str]) -> str:
    """Record one comment body and hand it back for the summary check."""
    if not isinstance(item, dict):
        return ""
    body = str(item.get("body") or "")
    bodies.append(body)
    return body


def _viewer(client: ForgeClient) -> str:
    """Who the publishing token speaks as, or ``""`` when it will not say.

    A CI token -- GitHub Actions' installation token, GitLab's ``CI_JOB_TOKEN``
    -- cannot read ``/user`` at all. An unanswerable question is not a mismatch,
    so the marker alone has to do there; treating it as one would report no
    published overview on every CI run and duplicate the comment each time.
    """
    try:
        who = client.get("/user")
    except SourceError as exc:
        log.debug("could not identify the publishing token (%s); trusting the marker", exc)
        return ""
    if not isinstance(who, dict):
        return ""
    key = "username" if client.target.provider == "gitlab" else "login"
    return str(who.get(key) or "")


def _author(item: dict[str, Any]) -> str:
    """The account a comment payload says wrote it, on either forge."""
    who = item.get("user") or item.get("author")
    if not isinstance(who, dict):
        return ""
    return str(who.get("login") or who.get("username") or "")


def _offer(
    candidates: list[SummaryRef],
    item: dict[str, Any],
    edit_root: str,
    method: str,
    body: str,
    viewer: str = "",
) -> None:
    """Note a body as roborak's overview, if that is what it is.

    The markers are published in plain sight, so a comment carrying one proves
    nothing on its own: anyone in the thread can paste it, and a match would
    then suppress the walkthrough and leave the copy standing as the review's
    overview. When the token names itself, the author has to be it.
    """
    identifier = item.get("id")
    if _SUMMARY_MARKER not in body or not isinstance(identifier, int):
        return
    if viewer and _author(item).casefold() != viewer.casefold():
        return
    found = _FLOW_RE.search(body)
    candidates.append(
        SummaryRef(
            edit_path=f"{edit_root}/{identifier}",
            method=method,
            flow=found.group(1) if found else "",
        )
    )
