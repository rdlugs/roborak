"""GitLab merge requests as a change source.

``diff_refs`` is the important part of the payload: GitLab anchors an inline
discussion to a *triple* of shas (base, start, head), and a comment posted with
the wrong triple either lands in the wrong place or is silently converted into an
unanchored note. We carry all three through on ``ForgeRef``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from roborak.context.diff import detect_language, parse_diff
from roborak.core.models import ChangedFile, ChangeSet, ChangeType, ForgeRef, Hunk
from roborak.sources.base import SourceError
from roborak.sources.forge import ForgeClient, Target

log = logging.getLogger(__name__)


@dataclass
class GitLabSource:
    target: Target
    token: str

    def load(self) -> ChangeSet:
        with ForgeClient(self.target, self.token) as client:
            base = f"/projects/{self.target.encoded_project}/merge_requests/{self.target.number}"
            merge_request = client.get(base)
            changes = client.get(f"{base}/changes")

        if not isinstance(merge_request, dict):
            raise SourceError("Unexpected merge request payload from GitLab.")

        refs = merge_request.get("diff_refs") or (changes or {}).get("diff_refs") or {}
        forge_ref = ForgeRef(
            provider="gitlab",
            host=self.target.host,
            project=self.target.project,
            number=self.target.number,
            base_sha=refs.get("base_sha"),
            start_sha=refs.get("start_sha"),
            head_sha=refs.get("head_sha"),
            web_url=merge_request.get("web_url"),
        )

        return ChangeSet(
            files=_files_from_changes((changes or {}).get("changes") or []),
            title=merge_request.get("title"),
            description=merge_request.get("description"),
            base_sha=refs.get("base_sha") or "",
            head_sha=refs.get("head_sha") or "",
            base_ref=merge_request.get("target_branch"),
            head_ref=merge_request.get("source_branch"),
            origin="gitlab",
            forge_ref=forge_ref,
        )


def _files_from_changes(changes: list[dict[str, object]]) -> list[ChangedFile]:
    """Rebuild ``ChangedFile`` objects from GitLab's per-file diff payload.

    GitLab hands back each file's diff body without the ``diff --git`` header, so
    we synthesise one and reuse the ordinary parser rather than keeping a second
    diff implementation alive.
    """
    files: list[ChangedFile] = []

    for change in changes:
        new_path = str(change.get("new_path") or "")
        old_path = str(change.get("old_path") or "")
        path = new_path or old_path
        if not path:
            continue

        body = str(change.get("diff") or "")
        hunks: list[Hunk] = []
        if body:
            synthetic = (
                f"diff --git a/{old_path or path} b/{new_path or path}\n"
                f"--- a/{old_path or path}\n+++ b/{new_path or path}\n{body}"
            )
            parsed = parse_diff(synthetic)
            hunks = parsed[0].hunks if parsed else []

        files.append(
            ChangedFile(
                path=path,
                previous_path=old_path if change.get("renamed_file") else None,
                change_type=_change_type(change),
                language=detect_language(path),
                hunks=hunks,
                is_binary=bool(change.get("diff") == "" and not change.get("deleted_file")),
            )
        )

    return files


def _change_type(change: dict[str, object]) -> ChangeType:
    if change.get("new_file"):
        return "added"
    if change.get("deleted_file"):
        return "deleted"
    if change.get("renamed_file"):
        return "renamed"
    return "modified"
