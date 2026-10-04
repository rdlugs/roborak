"""roborak's own open inline threads, read back off the forge.

Publishing has always been a one-way street: a finding went out and nothing ever
looked at it again. This is the return path. It finds the actionable threads an
earlier run opened and has not closed, so that a later run can say which commit
fixed one and resolve it.

Two rules shape everything here, and both are about what is *excluded*:

- **Only roborak's own actionable threads are eligible.** The fingerprint markers
  are published in plain sight, so a body carrying one proves nothing on its own;
  authorship is the test, exactly as it is for the summary comment in ``base``.
  Nitpicks, requirement gaps and unanchorable findings never had an inline thread
  in the first place (see ``core.buckets``), so they are excluded by construction
  rather than by a check that could rot.
- **A resolved thread is finished.** It never re-enters the list, whoever resolved
  it and for whatever reason, which is what makes a repeated run a no-op.

The same sweep collects reviewer dismissals: a human reply on one of those
threads, open or resolved, that starts a line with a configured marker. Only the
verdict, the replier's login and the finding's identity are kept; the reply text
itself never travels further, and certainly never into a prompt.

GitHub needs GraphQL for all of this. Its REST API cannot group review comments
into threads, will not say whether a thread is resolved, and has no endpoint at
all for resolving one. GitLab does the whole job over REST.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from roborak.core.severity import FeedbackVerdict
from roborak.render.markdown import RESOLUTION_MARKER_PREFIX
from roborak.sources.base import SourceError
from roborak.sources.discussion import is_bot
from roborak.sources.forge import ForgeClient, Target

log = logging.getLogger(__name__)

MAX_THREADS = 50
"""Threads considered in one run. A change with more open roborak threads than
this has a bigger problem than an unresolved comment."""

MAX_THREAD_BODY_CHARS = 4000
"""How much of a published finding is carried back into the verification prompt."""

MAX_DISMISSALS = 100
"""Dismissals collected in one run, so one busy change cannot flood local state."""

MAX_AUTHOR_CHARS = 100
MAX_TITLE_CHARS = 200

_FINDING_MARKER_RE = re.compile(r"<!--\s*roborak:v[12]:([0-9a-f]{16})\s*-->")
_RESOLUTION_MARKER_RE = re.compile(rf"<!--\s*{RESOLUTION_MARKER_PREFIX}:[0-9a-f]{{16}}\s*-->")
_TITLE_RE = re.compile(r"^\*\*(.+?)\*\*\s*$", re.MULTILINE)


@dataclass(frozen=True)
class OpenThread:
    """One unresolved inline thread roborak opened, and how to write back to it."""

    key: str
    """The forge's handle: a GitLab discussion id, a GitHub thread node id."""

    fingerprints: frozenset[str]
    body: str
    """The published finding itself, as markdown.

    This is what makes the whole feature work from a checkout that has never seen
    this merge request. The state directory holds fingerprints and nothing else,
    and in CI it does not exist at all, so the *only* durable record of what was
    reported is the comment that reported it."""

    file: str
    line: int
    anchor_sha: str
    """The revision the thread was anchored at -- where the commit range starts.
    Empty when the forge did not say, and the caller falls back to local state."""

    replied: bool
    """Whether an evidence reply is already on the thread. The reply and the
    resolve fail independently, so this is tracked apart from resolution: a run
    whose resolve call failed must retry that alone rather than saying it twice."""

    @property
    def location(self) -> str:
        return f"{self.file}:{self.line}" if self.line else self.file


@dataclass(frozen=True)
class Dismissal:
    """A human telling roborak, in a reply on its own thread, not to repeat a finding."""

    fingerprints: frozenset[str]
    verdict: FeedbackVerdict
    author: str
    file: str
    title: str
    recorded_at: str
    """When the reply was written, as the forge reported it."""


@dataclass(frozen=True)
class ThreadSweep:
    """Everything one pass over roborak's own threads found."""

    open: list[OpenThread] = field(default_factory=list)
    dismissals: list[Dismissal] = field(default_factory=list)


def open_threads(client: ForgeClient, target: Target, viewer: str) -> list[OpenThread]:
    """Every unresolved actionable thread roborak wrote, or an empty list.

    Never fatal. This is an enhancement to publishing, not a precondition for it:
    a forge that will not answer costs the run some resolutions, never the review.
    """
    return sweep_threads(client, target, viewer).open


def sweep_threads(
    client: ForgeClient,
    target: Target,
    viewer: str,
    markers: Mapping[str, FeedbackVerdict] | None = None,
) -> ThreadSweep:
    """roborak's open threads and, given ``markers``, the dismissals left on any of them.

    Never fatal, for the same reason as ``open_threads``: a forge that will not
    answer costs the run some resolutions and some suppressions, never the review.
    """
    try:
        if target.provider == "gitlab":
            return _gitlab_threads(client, target, viewer, markers or {})
        return _github_threads(client, target, viewer, markers or {})
    except SourceError as exc:
        log.warning("could not read open threads; leaving them alone: %s", exc)
        return ThreadSweep()


def _mine(author: object, viewer: str, provider: str) -> bool:
    """Whether roborak wrote this, under the same test the summary comment uses.

    When the publishing token can name itself, authorship must match it. A CI
    token that cannot read ``/user`` leaves only the weaker test the forge will
    still attest to -- that the account is a bot -- which is not proof it is
    *this* bot, but does put a forged thread out of reach of the contributors who
    can comment on the change.
    """
    if not viewer:
        return is_bot(author, provider=provider)
    if not isinstance(author, dict):
        return False
    name = str(author.get("login") or author.get("username") or "")
    return name.casefold() == viewer.casefold()


def _root_fingerprints(
    root_body: str, author: object, viewer: str, provider: str
) -> frozenset[str]:
    """The finding identities on a thread roborak opened, or nothing if it did not."""
    fingerprints = frozenset(_FINDING_MARKER_RE.findall(root_body))
    if not (fingerprints and _mine(author, viewer, provider)):
        return frozenset()
    return fingerprints


def verdict_in(body: str, markers: Mapping[str, FeedbackVerdict]) -> FeedbackVerdict | None:
    """The verdict a reply states, if a line of it starts with a configured marker.

    Anchored to the start of a line and to a word boundary after the marker, so a
    quoted reply (``> roborak: ignore``) or a longer word does not count.
    """
    folded = {marker.strip().casefold(): verdict for marker, verdict in markers.items()}
    for line in body.splitlines():
        text = line.strip().casefold()
        for marker, verdict in folded.items():
            if not text.startswith(marker):
                continue
            rest = text[len(marker) :]
            if not rest or not (rest[0].isalnum() or rest[0] in "-_"):
                return verdict
    return None


def _dismissal_from(
    fingerprints: frozenset[str],
    root_body: str,
    replies: list[dict[str, Any]],
    *,
    viewer: str,
    provider: str,
    file: object,
    markers: Mapping[str, FeedbackVerdict],
) -> Dismissal | None:
    """The latest marker reply a person left on one of roborak's threads."""
    if not (fingerprints and markers):
        return None
    latest: Dismissal | None = None
    for reply in replies:
        author = reply.get("author")
        if is_bot(author, provider=provider) or _mine(author, viewer, provider):
            continue
        verdict = verdict_in(str(reply.get("body") or ""), markers)
        if verdict is None:
            continue
        author_data = author if isinstance(author, dict) else {}
        name = str(author_data.get("login") or author_data.get("username") or "")
        created = str(reply.get("created_at") or "")
        if latest is not None and created < latest.recorded_at:
            continue
        title = _TITLE_RE.search(root_body)
        latest = Dismissal(
            fingerprints=fingerprints,
            verdict=verdict,
            author=name[:MAX_AUTHOR_CHARS],
            file=str(file or ""),
            title=(title.group(1).rstrip(".") if title else "")[:MAX_TITLE_CHARS],
            recorded_at=created,
        )
    return latest


def _thread_from(
    key: object,
    root_body: str,
    author: object,
    *,
    viewer: str,
    provider: str,
    file: object,
    line: object,
    anchor_sha: object,
    replied: bool,
) -> OpenThread | None:
    """One eligible thread, or ``None`` when anything about it disqualifies it."""
    fingerprints = _root_fingerprints(root_body, author, viewer, provider)
    if not (key and fingerprints):
        return None
    path = str(file or "")
    if not path:
        # An inline thread without a path cannot be checked against a file's
        # history, which is the only evidence this feature knows how to gather.
        return None
    return OpenThread(
        key=str(key),
        fingerprints=fingerprints,
        body=root_body[:MAX_THREAD_BODY_CHARS],
        file=path,
        line=line if isinstance(line, int) and not isinstance(line, bool) else 0,
        anchor_sha=str(anchor_sha or ""),
        replied=replied,
    )


def _gitlab_threads(
    client: ForgeClient,
    target: Target,
    viewer: str,
    markers: Mapping[str, FeedbackVerdict],
) -> ThreadSweep:
    """GitLab discussions, filtered the way ``sources.discussion`` filters them."""
    base = f"/projects/{target.encoded_project}/merge_requests/{target.number}"
    found = ThreadSweep()

    for discussion in client.paginate(f"{base}/discussions"):
        if len(found.open) == MAX_THREADS and (
            not markers or len(found.dismissals) == MAX_DISMISSALS
        ):
            break
        if not isinstance(discussion, dict):
            continue
        notes = [note for note in discussion.get("notes") or [] if isinstance(note, dict)]
        if not notes:
            continue

        root = notes[0]
        root_body = str(root.get("body") or "")
        position = root.get("position")
        position = position if isinstance(position, dict) else {}
        file = position.get("new_path") or position.get("old_path")

        if len(found.dismissals) < MAX_DISMISSALS and (
            dismissal := _dismissal_from(
                _root_fingerprints(root_body, root.get("author"), viewer, "gitlab"),
                root_body,
                notes[1:],
                viewer=viewer,
                provider="gitlab",
                file=file,
                markers=markers,
            )
        ):
            found.dismissals.append(dismissal)

        if (
            len(found.open) == MAX_THREADS
            or discussion.get("resolved")
            or any(note.get("resolvable") and note.get("resolved") for note in notes)
        ):
            continue
        thread = _thread_from(
            discussion.get("id"),
            root_body,
            root.get("author"),
            viewer=viewer,
            provider="gitlab",
            file=file,
            line=position.get("new_line"),
            anchor_sha=position.get("head_sha"),
            replied=any(_RESOLUTION_MARKER_RE.search(str(n.get("body") or "")) for n in notes),
        )
        if thread is not None:
            found.open.append(thread)
    return found


_THREADS_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      reviewThreads(first: 100) {
        nodes {
          id
          isResolved
          path
          line
          comments(first: 50) {
            nodes {
              body
              createdAt
              author { login __typename }
              originalCommit { oid }
            }
          }
        }
      }
    }
  }
}
"""


def _github_threads(
    client: ForgeClient,
    target: Target,
    viewer: str,
    markers: Mapping[str, FeedbackVerdict],
) -> ThreadSweep:
    """GitHub review threads, which exist only in GraphQL."""
    data = client.graphql(
        _THREADS_QUERY,
        {"owner": target.owner, "name": target.name, "number": target.number},
    )
    nodes = _dig(data, "repository", "pullRequest", "reviewThreads", "nodes")
    found = ThreadSweep()
    if not isinstance(nodes, list):
        return found

    for node in nodes:
        if len(found.open) == MAX_THREADS and (
            not markers or len(found.dismissals) == MAX_DISMISSALS
        ):
            break
        if not isinstance(node, dict):
            continue
        raw = _dig(node, "comments", "nodes")
        comments = (
            [_rest_shaped(c) for c in raw if isinstance(c, dict)] if isinstance(raw, list) else []
        )
        if not comments:
            continue

        root = comments[0]
        root_body = str(root.get("body") or "")

        if len(found.dismissals) < MAX_DISMISSALS and (
            dismissal := _dismissal_from(
                _root_fingerprints(root_body, root.get("author"), viewer, "github"),
                root_body,
                comments[1:],
                viewer=viewer,
                provider="github",
                file=node.get("path"),
                markers=markers,
            )
        ):
            found.dismissals.append(dismissal)

        if len(found.open) == MAX_THREADS or node.get("isResolved"):
            continue
        thread = _thread_from(
            node.get("id"),
            root_body,
            root.get("author"),
            viewer=viewer,
            provider="github",
            file=node.get("path"),
            line=node.get("line"),
            anchor_sha=_dig(root, "originalCommit", "oid"),
            replied=any(_RESOLUTION_MARKER_RE.search(str(c.get("body") or "")) for c in comments),
        )
        if thread is not None:
            found.open.append(thread)
    return found


def _rest_shaped(comment: dict[str, Any]) -> dict[str, Any]:
    """A GraphQL comment with the author and timestamp keys the REST helpers read.

    GraphQL types an account by ``__typename``; ``is_bot`` reads REST's ``type``.
    """
    author = comment.get("author")
    author = dict(author) if isinstance(author, dict) else {}
    author["type"] = author.pop("__typename", "")
    return {**comment, "author": author, "created_at": comment.get("createdAt") or ""}


def _dig(data: Any, *keys: str) -> Any:
    """Walk a GraphQL response, giving up quietly at the first thing that is not a map."""
    for key in keys:
        if not isinstance(data, dict):
            return None
        data = data.get(key)
    return data


_REPLY_MUTATION = """
mutation($thread: ID!, $body: String!) {
  addPullRequestReviewThreadReply(input: {pullRequestReviewThreadId: $thread, body: $body}) {
    comment { id }
  }
}
"""

_RESOLVE_MUTATION = """
mutation($thread: ID!) {
  resolveReviewThread(input: {threadId: $thread}) {
    thread { isResolved }
  }
}
"""


def reply(client: ForgeClient, target: Target, thread: OpenThread, body: str) -> None:
    """Post the evidence into the thread. Raises ``SourceError`` if it did not land."""
    if target.provider == "gitlab":
        base = f"/projects/{target.encoded_project}/merge_requests/{target.number}"
        client.post(f"{base}/discussions/{thread.key}/notes", {"body": body})
        return
    client.graphql(_REPLY_MUTATION, {"thread": thread.key, "body": body})


def resolve(client: ForgeClient, target: Target, thread: OpenThread) -> None:
    """Mark the thread resolved. Raises ``SourceError`` if it did not take.

    Called only after ``reply`` has returned, never beside it: a thread closed
    without the evidence that closed it is the one outcome worse than a thread
    left open, because the reasoning disappears along with the comment.
    """
    if target.provider == "gitlab":
        base = f"/projects/{target.encoded_project}/merge_requests/{target.number}"
        client.put(f"{base}/discussions/{thread.key}", {"resolved": True})
        return
    client.graphql(_RESOLVE_MUTATION, {"thread": thread.key})
