"""Post a review onto a GitHub pull request.

GitHub takes the whole review in one call: a body plus an array of inline
comments, each anchored by ``path`` + ``line`` + ``side``. ``side: RIGHT`` means
the line is a new-file line number, which is exactly the coordinate roborak
carries, so no translation is needed beyond checking the line is in the diff.

Submitting as one review rather than N separate comments matters: it produces a
single notification instead of one per finding.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from roborak.core.buckets import can_anchor
from roborak.core.models import ChangeSet, Finding, ReviewResult
from roborak.publish.base import (
    PublishReport,
    finding_markdown,
    inline_findings,
    summarised_findings,
    summary_markdown,
)
from roborak.sources.base import SourceError
from roborak.sources.forge import ForgeClient, Target

log = logging.getLogger(__name__)


@dataclass
class GitHubPublisher:
    target: Target
    token: str
    post_inline: bool = True
    post_summary: bool = True
    seen_fingerprints: frozenset[str] = frozenset()

    def publish(self, result: ReviewResult) -> PublishReport:
        report = PublishReport()
        changeset = result.changeset
        if changeset is None or changeset.forge_ref is None:
            raise SourceError("Cannot publish: this review did not come from a pull request.")

        comments: list[dict[str, Any]] = []
        if self.post_inline:
            for finding in inline_findings(result):
                if {
                    finding.fingerprint,
                    finding.fingerprint_v2,
                } & self.seen_fingerprints:
                    report.skipped_duplicate.append(finding)
                    continue
                comment = self._comment_for(finding, changeset)
                if comment is None:
                    report.failed.append((finding, "no anchorable position in the diff"))
                    continue
                comments.append(comment)
                report.posted.append(finding)

        report.summarised.extend(summarised_findings(result))

        payload: dict[str, Any] = {
            "body": summary_markdown(result) if self.post_summary else "",
            "event": "COMMENT",
            "comments": comments,
        }
        if changeset.forge_ref.head_sha:
            payload["commit_id"] = changeset.forge_ref.head_sha

        if not comments and not self.post_summary:
            return report

        base = f"/repos/{self.target.project}/pulls/{self.target.number}/reviews"
        with ForgeClient(self.target, self.token) as client:
            try:
                client.post(base, payload)
            except SourceError as exc:
                log.warning("inline review rejected (%s); posting the summary only", exc)
                report.failed.extend((f, str(exc)) for f in report.posted)
                report.posted.clear()
                if self.post_summary:
                    client.post(
                        f"/repos/{self.target.project}/issues/{self.target.number}/comments",
                        {"body": summary_markdown(result)},
                    )
                    report.summary_posted = True
            else:
                report.summary_posted = self.post_summary

        return report

    def _comment_for(self, finding: Finding, changeset: ChangeSet) -> dict[str, Any] | None:
        if not can_anchor(finding, changeset):
            return None
        file = changeset.file_by_path(finding.file)
        assert file is not None

        comment: dict[str, Any] = {
            "path": file.path,
            "line": finding.end_line,
            "side": "RIGHT",
            "body": finding_markdown(finding),
        }
        if finding.end_line > finding.start_line and file.diff_position(finding.start_line):
            comment["start_line"] = finding.start_line
            comment["start_side"] = "RIGHT"
        return comment
