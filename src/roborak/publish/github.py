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

from roborak.core.models import Finding, ReviewResult
from roborak.publish.base import PublishReport, finding_markdown, summary_markdown
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
            for finding in result.sorted_findings():
                if finding.fingerprint in self.seen_fingerprints:
                    report.skipped_duplicate.append(finding)
                    continue
                comment = self._comment_for(finding, changeset)
                if comment is None:
                    report.failed.append((finding, "no anchorable position in the diff"))
                    continue
                comments.append(comment)
                report.posted.append(finding)

        payload: dict[str, Any] = {
            "body": summary_markdown(result) if self.post_summary else "",
            "event": "COMMENT",  # never approve or request changes on the user's behalf
            "comments": comments,
        }
        if changeset.forge_ref.head_sha:
            payload["commit_id"] = changeset.forge_ref.head_sha

        base = f"/repos/{self.target.project}/pulls/{self.target.number}/reviews"
        with ForgeClient(self.target, self.token) as client:
            try:
                client.post(base, payload)
            except SourceError as exc:
                # GitHub rejects the entire review if any one anchor is invalid,
                # so fall back to the summary alone rather than losing everything.
                log.warning("inline review rejected (%s); posting the summary only", exc)
                report.failed.extend((f, str(exc)) for f in report.posted)
                report.posted.clear()
                client.post(
                    f"/repos/{self.target.project}/issues/{self.target.number}/comments",
                    {"body": summary_markdown(result)},
                )
            report.summary_posted = self.post_summary

        return report

    def _comment_for(self, finding: Finding, changeset: Any) -> dict[str, Any] | None:
        file = changeset.file_by_path(finding.file)
        if file is None or file.diff_position(finding.start_line) is None:
            return None

        comment: dict[str, Any] = {
            "path": file.path,
            "line": finding.end_line,
            "side": "RIGHT",
            "body": finding_markdown(finding),
        }
        # A multi-line comment needs both ends inside the diff.
        if finding.end_line > finding.start_line and file.diff_position(finding.start_line):
            comment["start_line"] = finding.start_line
            comment["start_side"] = "RIGHT"
        return comment
