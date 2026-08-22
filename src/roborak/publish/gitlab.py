"""Post a review onto a GitLab merge request.

Position semantics, which are the whole reason this file is delicate:
GitLab anchors a diff discussion with ``base_sha``/``start_sha``/``head_sha``
plus ``new_path`` and ``new_line``. All three shas must come from the MR's
``diff_refs``; substituting the repository's own shas produces a comment that
either lands on the wrong line or silently degrades into an unanchored note.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from roborak.core.buckets import can_anchor
from roborak.core.models import ChangeSet, Finding, ForgeRef, ReviewResult
from roborak.publish.base import (
    PublishReport,
    SummaryRef,
    finding_markdown,
    inline_findings,
    publish_summary,
    summarised_findings,
)
from roborak.sources.base import SourceError
from roborak.sources.forge import ForgeClient, Target

log = logging.getLogger(__name__)


@dataclass
class GitLabPublisher:
    target: Target
    token: str
    post_inline: bool = True
    post_summary: bool = True
    seen_fingerprints: frozenset[str] = frozenset()
    summary_ref: SummaryRef | None = None
    """An overview an earlier run published, to edit rather than duplicate."""

    summary_refreshed: bool = False
    """Whether the overview being published is a new narration of a change that
    has moved, which the edited comment says out loud."""

    def publish(self, result: ReviewResult) -> PublishReport:
        report = PublishReport()
        changeset = result.changeset
        if changeset is None or changeset.forge_ref is None:
            raise SourceError("Cannot publish: this review did not come from a merge request.")

        base = f"/projects/{self.target.encoded_project}/merge_requests/{self.target.number}"
        with ForgeClient(self.target, self.token) as client:
            if self.post_inline:
                for finding in inline_findings(result):
                    self._post_one(client, base, finding, changeset, changeset.forge_ref, report)

            report.summarised.extend(summarised_findings(result))

            if self.post_summary:
                publish_summary(
                    client,
                    result,
                    report,
                    post_path=f"{base}/notes",
                    ref=self.summary_ref,
                    refreshed=self.summary_refreshed,
                )

        return report

    def _post_one(
        self,
        client: ForgeClient,
        base: str,
        finding: Finding,
        changeset: ChangeSet,
        forge_ref: ForgeRef,
        report: PublishReport,
    ) -> None:
        if {finding.fingerprint, finding.fingerprint_v2} & self.seen_fingerprints:
            report.skipped_duplicate.append(finding)
            return

        payload = self._discussion_payload(finding, changeset, forge_ref)
        if payload is None:
            report.failed.append((finding, "no anchorable position in the diff"))
            return

        try:
            client.post(f"{base}/discussions", payload)
        except SourceError as exc:
            log.warning("could not post %s: %s", finding.location, exc)
            report.failed.append((finding, str(exc)))
            return

        report.posted.append(finding)

    def _discussion_payload(
        self, finding: Finding, changeset: ChangeSet, forge_ref: ForgeRef
    ) -> dict[str, Any] | None:
        if not can_anchor(finding, changeset):
            return None
        if not (forge_ref.base_sha and forge_ref.head_sha):
            return None
        file = changeset.file_by_path(finding.file)
        assert file is not None

        body = finding_markdown(finding, suggestion_syntax=_suggestion_spec(finding))

        payload: dict[str, Any] = {
            "body": body,
            "position[position_type]": "text",
            "position[base_sha]": forge_ref.base_sha,
            "position[start_sha]": forge_ref.start_sha or forge_ref.base_sha,
            "position[head_sha]": forge_ref.head_sha,
            "position[new_path]": file.path,
            "position[new_line]": finding.start_line,
        }
        payload["position[old_path]"] = file.previous_path or file.path
        return payload


def _suggestion_spec(finding: Finding) -> str:
    """GitLab's suggestion fence carries the range it replaces.

    ``suggestion:-0+N`` means "this line plus the N following it", relative to the
    line the discussion is anchored to.
    """
    if not finding.suggestion:
        return "suggestion"
    span = finding.end_line - finding.start_line
    return f"suggestion:-0+{span}" if span else "suggestion:-0+0"
