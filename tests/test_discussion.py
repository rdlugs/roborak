"""Forge discussion is useful context only after aggressive noise control."""

from __future__ import annotations

import logging

from roborak.sources.base import SourceError
from roborak.sources.discussion import (
    MAX_DISCUSSION_CHARS,
    MAX_DISCUSSION_COMMENT_CHARS,
    MAX_DISCUSSION_COMMENTS,
    load_change_discussions,
)
from roborak.sources.forge import Target


class StubClient:
    def __init__(self, payloads: dict[str, object]) -> None:
        self.payloads = payloads

    def paginate(self, path: str, **params: object) -> list[object]:
        payload = self.payloads.get(path, [])
        if isinstance(payload, Exception):
            raise payload
        assert isinstance(payload, list)
        return payload


def test_gitlab_keeps_only_current_unresolved_human_discussion():
    target = Target("gitlab", "gitlab.com", "acme/web", 7)
    path = "/projects/acme%2Fweb/merge_requests/7/discussions"
    client = StubClient(
        {
            path: [
                {
                    "notes": [
                        {
                            "body": "Please retain deterministic ordering.",
                            "author": {"username": "sam", "bot": False},
                            "created_at": "2026-08-20T01:00:00Z",
                            "position": {
                                "head_sha": "head2",
                                "new_path": "app/routes.py",
                                "new_line": 18,
                            },
                        }
                    ]
                },
                {
                    "resolved": True,
                    "notes": [{"body": "Already fixed", "author": {"username": "lee"}}],
                },
                {
                    "notes": [
                        {
                            "body": "changed the title",
                            "system": True,
                            "author": {"username": "system"},
                        },
                        {
                            "body": "Automated review",
                            "author": {"username": "bot", "bot": True},
                        },
                        {
                            "body": "Old diff note",
                            "author": {"username": "sam"},
                            "position": {"head_sha": "head1"},
                        },
                        {
                            "body": "<!-- roborak:review -->\nPrior review",
                            "author": {"username": "sam"},
                        },
                    ]
                },
                {
                    "notes": [
                        {
                            "body": "Why are public routes loaded last?",
                            "author": {"username": "pat"},
                            "created_at": "2026-08-20T02:00:00Z",
                        }
                    ]
                },
            ]
        }
    )

    comments = load_change_discussions(client, target, head_sha="head2")  # type: ignore[arg-type]

    assert [comment.body for comment in comments] == [
        "Please retain deterministic ordering.",
        "Why are public routes loaded last?",
    ]
    assert comments[0].author == "sam"
    assert (comments[0].path, comments[0].line) == ("app/routes.py", 18)


def test_github_combines_conversation_reviews_and_current_inline_comments():
    target = Target("github", "github.com", "acme/web", 9)
    root = "/repos/acme/web"
    client = StubClient(
        {
            f"{root}/issues/9/comments": [
                {
                    "body": "Please add a route parity test.",
                    "user": {"login": "sam", "type": "User"},
                    "created_at": "2026-08-20T01:00:00Z",
                },
                {
                    "body": "Automated",
                    "user": {"login": "ci[bot]", "type": "Bot"},
                },
            ],
            f"{root}/pulls/9/reviews": [
                {
                    "body": "Ordering is the main risk.",
                    "state": "CHANGES_REQUESTED",
                    "user": {"login": "lee", "type": "User"},
                    "submitted_at": "2026-08-20T02:00:00Z",
                },
                {
                    "body": "Draft review",
                    "state": "PENDING",
                    "user": {"login": "lee", "type": "User"},
                },
            ],
            f"{root}/pulls/9/comments": [
                {
                    "body": "This middleware order changed.",
                    "position": 4,
                    "path": "app/routes.py",
                    "line": 22,
                    "user": {"login": "pat", "type": "User"},
                    "created_at": "2026-08-20T03:00:00Z",
                },
                {
                    "body": "Outdated line",
                    "position": None,
                    "user": {"login": "pat", "type": "User"},
                },
            ],
        }
    )

    comments = load_change_discussions(client, target)  # type: ignore[arg-type]

    assert [comment.body for comment in comments] == [
        "Please add a route parity test.",
        "Ordering is the main risk.",
        "This middleware order changed.",
    ]
    assert (comments[-1].path, comments[-1].line) == ("app/routes.py", 22)


def test_discussion_context_is_bounded_and_keeps_the_newest_comments():
    target = Target("github", "github.com", "acme/web", 9)
    root = "/repos/acme/web"
    items = [
        {
            "body": f"comment {index:02d} " + "x" * 700,
            "user": {"login": "sam", "type": "User"},
            "created_at": f"2026-08-20T{index:02d}:00:00Z",
        }
        for index in range(25)
    ]
    client = StubClient({f"{root}/issues/9/comments": items})

    comments = load_change_discussions(client, target)  # type: ignore[arg-type]

    assert len(comments) <= MAX_DISCUSSION_COMMENTS
    assert sum(len(comment.body) for comment in comments) <= MAX_DISCUSSION_CHARS
    assert all(len(comment.body) <= MAX_DISCUSSION_COMMENT_CHARS for comment in comments)
    assert comments[-1].body.startswith("comment 24")
    assert [comment.created_at for comment in comments] == sorted(
        comment.created_at for comment in comments
    )


def test_discussion_failure_is_non_fatal(caplog):
    target = Target("gitlab", "gitlab.com", "acme/web", 7)
    path = "/projects/acme%2Fweb/merge_requests/7/discussions"
    client = StubClient({path: SourceError("not permitted")})

    with caplog.at_level(logging.WARNING):
        comments = load_change_discussions(client, target)  # type: ignore[arg-type]

    assert comments == []
    assert "continuing without them" in caplog.text
