"""Tracker issues as review context, and as a way to find the change to review.

Two jobs, both built on ``ForgeClient`` so auth, pagination and the 401/403/404
error strings match every other forge call:

* **load the issue** -- title, body, labels and human discussion, so the review can
  judge the change against what was actually asked for.
* **resolve what implements it** -- the merge or pull request linked to the issue,
  so ``--issue 42`` alone is enough to review the right diff.

Everything here is capped. Issue context rides in the prompt's scaffolding
headroom rather than the chunker's budget (which measures only file diffs), so an
issue with two hundred comments must not be allowed to eat the context window.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from roborak.core.models import Issue
from roborak.sources.base import SourceError
from roborak.sources.forge import ForgeClient, Target

log = logging.getLogger(__name__)

MAX_ISSUE_COMMENTS = 20
MAX_COMMENT_CHARS = 600
MAX_ISSUE_CHARS = 4000
"""Ceiling on body plus comments combined, applied after per-comment truncation."""


@dataclass
class LinkedChange:
    """A merge or pull request that claims to implement an issue."""

    number: int
    state: str = ""
    updated_at: str = ""
    web_url: str | None = None

    @property
    def is_open(self) -> bool:
        return self.state.lower() in {"open", "opened", "reopened"}


@dataclass
class GitLabIssueSource:
    target: Target
    token: str

    @property
    def _base(self) -> str:
        return f"/projects/{self.target.encoded_project}/issues/{self.target.number}"

    def load(self) -> Issue:
        with ForgeClient(self.target, self.token) as client:
            payload = client.get(self._base)
            notes = client.paginate(f"{self._base}/notes", sort="asc", order_by="created_at")

        if not isinstance(payload, dict):
            raise SourceError("Unexpected issue payload from GitLab.")

        labels = [str(label) for label in payload.get("labels") or [] if label]
        return _build_issue(
            self.target,
            title=payload.get("title"),
            body=payload.get("description"),
            labels=labels,
            state=payload.get("state"),
            web_url=payload.get("web_url"),
            comments=[n.get("body") for n in notes if isinstance(n, dict) and not n.get("system")],
        )

    def linked_changes(self) -> list[LinkedChange]:
        with ForgeClient(self.target, self.token) as client:
            related = client.paginate(f"{self._base}/related_merge_requests")

        changes: list[LinkedChange] = []
        for entry in related:
            if not isinstance(entry, dict):
                continue
            iid = entry.get("iid")
            if not isinstance(iid, int):
                continue
            if not _same_gitlab_project(entry, self.target):
                log.debug("skipping merge request !%s: different project", iid)
                continue
            changes.append(
                LinkedChange(
                    number=iid,
                    state=str(entry.get("state") or ""),
                    updated_at=str(entry.get("updated_at") or ""),
                    web_url=_as_optional_str(entry.get("web_url")),
                )
            )
        return changes


@dataclass
class GitHubIssueSource:
    target: Target
    token: str

    @property
    def _base(self) -> str:
        return f"/repos/{self.target.project}/issues/{self.target.number}"

    def load(self) -> Issue:
        with ForgeClient(self.target, self.token) as client:
            payload = client.get(self._base)
            comments = client.paginate(f"{self._base}/comments")

        if not isinstance(payload, dict):
            raise SourceError("Unexpected issue payload from GitHub.")
        if payload.get("pull_request"):
            raise SourceError(
                f"#{self.target.number} is a pull request, not an issue - "
                f"use --pr {self.target.number}."
            )

        labels = [
            str(label.get("name"))
            for label in payload.get("labels") or []
            if isinstance(label, dict) and label.get("name")
        ]
        return _build_issue(
            self.target,
            title=payload.get("title"),
            body=payload.get("body"),
            labels=labels,
            state=payload.get("state"),
            web_url=payload.get("html_url"),
            comments=[c.get("body") for c in comments if isinstance(c, dict)],
        )

    def linked_changes(self) -> list[LinkedChange]:
        """Read linked pull requests off the issue's timeline.

        REST's ``connected`` event does not carry the pull request's number, so
        ``cross-referenced`` events are the only usable signal; a "Fixes #42" in a
        pull request body produces one.
        """
        with ForgeClient(self.target, self.token) as client:
            events = client.paginate(f"{self._base}/timeline")

        seen: dict[int, LinkedChange] = {}
        for event in events:
            if not isinstance(event, dict) or event.get("event") != "cross-referenced":
                continue
            source = event.get("source")
            issue = source.get("issue") if isinstance(source, dict) else None
            if not isinstance(issue, dict) or not issue.get("pull_request"):
                continue

            repository = issue.get("repository")
            full_name = repository.get("full_name") if isinstance(repository, dict) else None
            if full_name and str(full_name) != self.target.project:
                log.debug("skipping cross-reference from %s", full_name)
                continue

            number = issue.get("number")
            if not isinstance(number, int):
                continue
            seen[number] = LinkedChange(
                number=number,
                state=str(issue.get("state") or ""),
                updated_at=str(issue.get("updated_at") or ""),
                web_url=_as_optional_str(issue.get("html_url")),
            )
        return list(seen.values())


def load_issue(target: Target, token: str) -> Issue:
    source = GitLabIssueSource if target.provider == "gitlab" else GitHubIssueSource
    return source(target=target, token=token).load()


def resolve_linked_change(target: Target, token: str) -> LinkedChange | None:
    """The change most likely to be the one implementing this issue.

    Open beats closed -- a merged predecessor is history, not the thing under
    review -- and within that, most recently updated wins.
    """
    source = GitLabIssueSource if target.provider == "gitlab" else GitHubIssueSource
    changes = source(target=target, token=token).linked_changes()
    if not changes:
        return None
    return max(changes, key=lambda c: (c.is_open, c.updated_at, c.number))


def _build_issue(
    target: Target,
    *,
    title: Any,
    body: Any,
    labels: list[str],
    state: Any,
    web_url: Any,
    comments: list[Any],
) -> Issue:
    text = _as_str(body)
    kept: list[str] = []
    budget = MAX_ISSUE_CHARS - len(text)

    for raw in comments[:MAX_ISSUE_COMMENTS]:
        comment = _as_str(raw)
        if not comment:
            continue
        comment = _truncate(comment, MAX_COMMENT_CHARS)
        if len(comment) > budget:
            break
        kept.append(comment)
        budget -= len(comment)

    return Issue(
        provider=target.provider,
        host=target.host,
        project=target.project,
        number=target.number,
        title=_as_str(title),
        body=_truncate(text, MAX_ISSUE_CHARS),
        labels=labels,
        state=_as_str(state),
        web_url=_as_optional_str(web_url),
        comments=kept,
    )


def _as_str(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _as_optional_str(value: Any) -> str | None:
    return str(value) if value else None


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit].rstrip() + " […]"


def _same_gitlab_project(entry: dict[str, Any], target: Target) -> bool:
    """GitLab's related-MR payload names the project by path reference.

    ``references.full`` is ``group/project!7``; anything before the ``!`` is the
    project this merge request lives in. When the payload says nothing at all we
    keep the merge request, since the endpoint is scoped to the issue's project.
    """
    references = entry.get("references")
    full = references.get("full") if isinstance(references, dict) else None
    if not full:
        return True
    return str(full).split("!", 1)[0] == target.project
