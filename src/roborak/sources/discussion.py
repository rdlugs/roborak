"""Bounded human discussion from a merge request or pull request.

Discussion is useful context, not authority. The source adapters discard resolved,
stale, automated, and roborak-authored material before it can consume prompt space.
The model prompt applies the same untrusted-input boundary as it does to diffs and
issue text.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any

from roborak.core.models import ReviewComment
from roborak.sources.base import SourceError
from roborak.sources.forge import ForgeClient, Target

log = logging.getLogger(__name__)

MAX_DISCUSSION_COMMENTS = 20
MAX_DISCUSSION_COMMENT_CHARS = 600
MAX_DISCUSSION_CHARS = 4000
ROBORAK_REVIEW_MARKER = "<!-- roborak:review -->"


def load_change_discussions(
    client: ForgeClient, target: Target, *, head_sha: str = ""
) -> list[ReviewComment]:
    """Fetch eligible discussion without making its failure fatal to a review."""
    try:
        comments = (
            _gitlab_comments(client, target, head_sha=head_sha)
            if target.provider == "gitlab"
            else _github_comments(client, target)
        )
    except SourceError as exc:
        log.warning("could not load change discussions; continuing without them: %s", exc)
        return []
    return _bounded(comments)


def _gitlab_comments(client: ForgeClient, target: Target, *, head_sha: str) -> list[ReviewComment]:
    base = f"/projects/{target.encoded_project}/merge_requests/{target.number}"
    discussions = client.paginate(f"{base}/discussions")
    kept: list[ReviewComment] = []

    for discussion in discussions:
        if not isinstance(discussion, dict):
            continue
        notes = [note for note in discussion.get("notes") or [] if isinstance(note, dict)]
        if discussion.get("resolved") or any(
            note.get("resolvable") and note.get("resolved") for note in notes
        ):
            continue
        for note in notes:
            if note.get("system") or is_bot(note.get("author"), provider="gitlab"):
                continue
            position = note.get("position")
            position = position if isinstance(position, dict) else None
            if position and head_sha and position.get("head_sha") not in {None, "", head_sha}:
                continue
            if comment := _comment_from(
                note,
                provider="gitlab",
                position=position,
                created_key="created_at",
            ):
                kept.append(comment)
    return kept


def _github_comments(client: ForgeClient, target: Target) -> list[ReviewComment]:
    root = f"/repos/{target.project}"
    comments: list[ReviewComment] = []

    for item in client.paginate(f"{root}/issues/{target.number}/comments"):
        if isinstance(item, dict) and (
            comment := _comment_from(item, provider="github", created_key="created_at")
        ):
            comments.append(comment)

    for review in client.paginate(f"{root}/pulls/{target.number}/reviews"):
        if not isinstance(review, dict) or str(review.get("state") or "").upper() == "PENDING":
            continue
        if comment := _comment_from(review, provider="github", created_key="submitted_at"):
            comments.append(comment)

    for item in client.paginate(f"{root}/pulls/{target.number}/comments"):
        if not isinstance(item, dict) or item.get("position") is None:
            continue
        if comment := _comment_from(
            item,
            provider="github",
            position=item,
            created_key="created_at",
        ):
            comments.append(comment)

    return comments


def _comment_from(
    item: dict[str, Any],
    *,
    provider: str,
    created_key: str,
    position: dict[str, Any] | None = None,
) -> ReviewComment | None:
    author = item.get("author") if provider == "gitlab" else item.get("user")
    if is_bot(author, provider=provider):
        return None
    body = str(item.get("body") or "").strip()
    if not body or _is_roborak(body):
        return None
    author_data = author if isinstance(author, dict) else {}
    author_name = author_data.get("username") or author_data.get("login") or ""
    location = position or {}
    path = location.get("new_path") or location.get("path") or location.get("old_path")
    line = location.get("new_line") or location.get("line") or location.get("original_line")
    return ReviewComment(
        author=str(author_name),
        body=body,
        path=str(path) if path else None,
        line=line if isinstance(line, int) and not isinstance(line, bool) else None,
        created_at=str(item.get(created_key) or ""),
    )


def is_bot(author: object, *, provider: str) -> bool:
    """Whether the forge itself says this account is a bot, not a person."""
    if not isinstance(author, dict):
        return False
    if provider == "gitlab":
        return bool(author.get("bot"))
    return str(author.get("type") or "").lower() == "bot"


def _is_roborak(body: str) -> bool:
    return ROBORAK_REVIEW_MARKER in body or "<!-- roborak:v1:" in body or "<!-- roborak:v2:" in body


def _bounded(comments: Iterable[ReviewComment]) -> list[ReviewComment]:
    ordered = sorted(comments, key=lambda comment: comment.created_at)
    recent = ordered[-MAX_DISCUSSION_COMMENTS:]
    remaining = MAX_DISCUSSION_CHARS
    selected: list[ReviewComment] = []

    for comment in reversed(recent):
        if remaining <= 0:
            break
        limit = min(MAX_DISCUSSION_COMMENT_CHARS, remaining)
        body = _truncate(comment.body, limit)
        if not body:
            continue
        selected.append(comment.model_copy(update={"body": body}))
        remaining -= len(body)
    selected.reverse()
    return selected


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    if limit <= 4:
        return text[:limit]
    return text[: limit - 4].rstrip() + " […]"
