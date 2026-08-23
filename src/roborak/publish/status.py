"""The verdict as something the forge itself can gate on.

A verdict stated in a comment is read by people; a commit status is read by branch
protection and merge-request approval rules. Both say the same thing because both
come from ``core.verdict``.

Deliberately a commit status rather than a GitHub check run: check runs need
``checks:write``, which GitHub grants only to Apps, and roborak authenticates as
whoever is running it -- a PAT, or ``gh auth token``. A commit status needs
``statuses:write``, which a classic PAT's ``repo`` scope already covers.
"""

from __future__ import annotations

import logging

from roborak.core.models import ReviewResult
from roborak.core.verdict import Gate, Verdict
from roborak.sources.base import SourceError
from roborak.sources.forge import ForgeClient, Target

log = logging.getLogger(__name__)

STATUS_CONTEXT = "roborak/review"
"""Names the status on both forges.

Re-posting under the same context on the same commit *replaces* the previous
status rather than appending one, which is the whole of "re-running a review
updates the check instead of stacking duplicates" -- no bookkeeping required.
It is also the string a user types into a branch-protection rule, so it must
not drift.
"""

DESCRIPTION_LIMIT = 140
"""GitHub truncates a status description past this; do it ourselves so the text
ends on a word rather than mid-sentence."""

_GITHUB_STATE: dict[Verdict, str] = {
    Verdict.PASS: "success",
    Verdict.BLOCKED: "failure",
    Verdict.ERROR: "error",
}

_GITLAB_STATE: dict[Verdict, str] = {
    Verdict.PASS: "success",
    Verdict.BLOCKED: "failed",
    Verdict.ERROR: "failed",
}
"""GitLab spells failure ``failed`` and has no separate ``error`` state, so an
inconclusive review fails rather than silently passing."""

_ALREADY_SET = {400, 409}
"""The codes GitLab answers with when it refuses a status transition -- but they
also cover a malformed request and a concurrent update, so the code alone is not
enough to call it 'already correct'; see ``_unchanged``."""

_UNCHANGED_MESSAGE = "cannot transition status"
"""What GitLab says when it refuses a status transition:
``Cannot transition status via :run from :running``. The message names a state
machine, not the state the commit ended up in, so it is only the first half of
the test -- ``_unchanged`` reads the status back before believing it."""


def post_status(
    client: ForgeClient,
    target: Target,
    result: ReviewResult,
    gate: Gate,
    summary_url: str | None = None,
) -> str | None:
    """Post the verdict as a commit status on the change's head commit.

    Returns ``None`` when it was posted, or a human-readable reason when it was
    skipped. Skipping is never fatal: a token that may comment but not set a
    status should still publish the review, with the gap reported rather than the
    whole run lost.
    """
    sha = _head_sha(result)
    if not sha:
        return "no head commit sha for this change"

    payload = _payload(target, gate, result, summary_url)
    path = (
        f"/projects/{target.encoded_project}/statuses/{sha}"
        if target.provider == "gitlab"
        else f"/repos/{target.project}/statuses/{sha}"
    )
    try:
        client.post(path, payload)
    except SourceError as exc:
        if target.provider == "gitlab" and _unchanged(client, target, sha, payload, exc):
            return None
        log.warning("could not post the pre-merge check: %s", exc)
        return _reason(exc)
    return None


def _unchanged(
    client: ForgeClient,
    target: Target,
    sha: str,
    payload: dict[str, str],
    exc: SourceError,
) -> bool:
    """Whether the refused post left the commit already saying what we wanted.

    A refusal is suppressed only when the commit is then observed carrying our
    context in the state we asked for. The status code is not proof -- 400 and
    409 also cover a malformed request and a concurrent update -- and neither is
    GitLab's message, which names a state machine rather than the state the
    commit is in. Reporting a check the merge request never got is the one
    failure a gate cannot absorb: branch protection would wait on a status that
    is absent, or read a stale verdict as the current one.

    An unreadable answer counts as changed. Silence about the state is not
    evidence the state is right.
    """
    if exc.status not in _ALREADY_SET or _UNCHANGED_MESSAGE not in str(exc).lower():
        return False
    try:
        found = client.get(
            f"/projects/{target.encoded_project}/repository/commits/{sha}/statuses",
            name=STATUS_CONTEXT,
        )
    except SourceError as read_back:
        log.debug("could not read the existing status back (%s)", read_back)
        return False
    if not isinstance(found, list):
        return False
    return any(
        isinstance(item, dict)
        and item.get("name") == STATUS_CONTEXT
        and item.get("status") == payload["state"]
        for item in found
    )


def _head_sha(result: ReviewResult) -> str:
    """The commit the status hangs on.

    ``ForgeRef.head_sha`` is the authoritative one and is what inline comments
    anchor to, but it is optional; ``ChangeSet.head_sha`` is the fallback for a
    change whose ref did not carry one.
    """
    changeset = result.changeset
    if changeset is None:
        return ""
    ref = changeset.forge_ref
    return (ref.head_sha if ref else None) or changeset.head_sha or ""


def _payload(
    target: Target, gate: Gate, result: ReviewResult, summary_url: str | None = None
) -> dict[str, str]:
    description = _truncate(gate.summary_line(), DESCRIPTION_LIMIT)
    if target.provider == "gitlab":
        payload = {
            "state": _GITLAB_STATE[gate.verdict],
            "name": STATUS_CONTEXT,
            "description": description,
        }
    else:
        payload = {
            "state": _GITHUB_STATE[gate.verdict],
            "context": STATUS_CONTEXT,
            "description": description,
        }
    changeset = result.changeset
    ref = changeset.forge_ref if changeset else None
    # The summary comment is the review; the merge request page is only where to
    # start looking for it, so it is the fallback when the comment has no URL.
    link = summary_url or (ref.web_url if ref else None)
    if link:
        payload["target_url"] = link
    return payload


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _reason(exc: SourceError) -> str:
    """Why the status did not go up, in terms the reader can act on."""
    if exc.status in {401, 403}:
        return (
            "the token may not set commit statuses "
            "(GitHub needs 'statuses:write' or the classic 'repo' scope; GitLab needs 'api')"
        )
    if exc.status == 404:
        return "the forge did not recognise the commit or the project"
    return str(exc)
