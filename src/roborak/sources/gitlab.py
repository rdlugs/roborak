"""GitLab merge requests as a change source.

``diff_refs`` is the important part of the payload: GitLab anchors an inline
discussion to a *triple* of shas (base, start, head), and a comment posted with
the wrong triple either lands in the wrong place or is silently converted into an
unanchored note. We carry all three through on ``ForgeRef``.
"""

from __future__ import annotations

import difflib
import logging
from dataclasses import dataclass
from urllib.parse import quote

from roborak.context.diff import detect_language, parse_diff
from roborak.core.models import ChangedFile, ChangeSet, ChangeType, ForgeRef, Hunk
from roborak.sources.base import SourceError
from roborak.sources.discussion import load_change_discussions
from roborak.sources.forge import ForgeClient, Recovery, Target

log = logging.getLogger(__name__)


@dataclass
class GitLabSource:
    target: Target
    token: str
    max_recovered_file_bytes: int = 1_048_576
    include_discussions: bool = True

    def load(self) -> ChangeSet:
        with ForgeClient(self.target, self.token) as client:
            base = f"/projects/{self.target.encoded_project}/merge_requests/{self.target.number}"
            merge_request = client.get(base)
            changes = client.get(f"{base}/changes")
            if not isinstance(merge_request, dict):
                raise SourceError("Unexpected merge request payload from GitLab.")

            refs = merge_request.get("diff_refs") or (changes or {}).get("diff_refs") or {}
            raw_changes = (changes or {}).get("changes") or []
            if isinstance(changes, dict) and changes.get("overflow"):
                log.warning("GitLab changes payload overflowed; fetching paginated diffs")
                raw_changes = client.paginate(f"{base}/diffs")
            files = _files_from_changes(
                raw_changes,
                client=client,
                target=self.target,
                base_sha=refs.get("base_sha") or "",
                head_sha=refs.get("head_sha") or "",
                max_bytes=self.max_recovered_file_bytes,
            )
            discussions = (
                load_change_discussions(client, self.target, head_sha=refs.get("head_sha") or "")
                if self.include_discussions
                else []
            )
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
            files=files,
            title=merge_request.get("title"),
            description=merge_request.get("description"),
            base_sha=refs.get("base_sha") or "",
            head_sha=refs.get("head_sha") or "",
            base_ref=merge_request.get("target_branch"),
            head_ref=merge_request.get("source_branch"),
            origin="gitlab",
            forge_ref=forge_ref,
            discussions=discussions,
        )


def _files_from_changes(
    changes: list[dict[str, object]],
    *,
    client: ForgeClient | None = None,
    target: Target | None = None,
    base_sha: str = "",
    head_sha: str = "",
    max_bytes: int = 1_048_576,
) -> list[ChangedFile]:
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

        is_binary = False
        zero_byte = False
        patch_unavailable = False
        change_type = _change_type(change)
        if not hunks and body == "":
            if path.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".pdf", ".zip")):
                is_binary = True
            elif client is not None and target is not None:
                try:
                    recovered = _recover_patch(
                        client,
                        target,
                        old_path or path,
                        new_path or path,
                        change_type,
                        base_sha,
                        head_sha,
                        max_bytes,
                    )
                    if recovered is None:
                        is_binary = True
                    else:
                        hunks = recovered.hunks
                        zero_byte = recovered.zero_byte
                        patch_unavailable = not hunks and not zero_byte
                except SourceError as exc:
                    log.warning("could not recover GitLab patch for %s: %s", path, exc)
                    patch_unavailable = True
            else:
                is_binary = True

        files.append(
            ChangedFile(
                path=path,
                previous_path=old_path if change.get("renamed_file") else None,
                change_type=change_type,
                language=detect_language(path),
                hunks=hunks,
                is_binary=is_binary,
                zero_byte=zero_byte,
                patch_unavailable=patch_unavailable,
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


def _recover_patch(
    client: ForgeClient,
    target: Target,
    old_path: str,
    new_path: str,
    change_type: ChangeType,
    base_sha: str,
    head_sha: str,
    max_bytes: int,
) -> Recovery | None:
    old = b"" if change_type == "added" else _gitlab_content(client, target, old_path, base_sha)
    new = b"" if change_type == "deleted" else _gitlab_content(client, target, new_path, head_sha)
    if len(old) > max_bytes or len(new) > max_bytes:
        raise SourceError(f"file exceeds recovery limit of {max_bytes} bytes")
    if b"\0" in old or b"\0" in new:
        return None
    try:
        old_text, new_text = old.decode(), new.decode()
    except UnicodeDecodeError:
        return None
    if not old_text and not new_text:
        return Recovery([], zero_byte=True)
    body = "\n".join(
        difflib.unified_diff(
            old_text.splitlines(),
            new_text.splitlines(),
            fromfile=f"a/{old_path}",
            tofile=f"b/{new_path}",
            lineterm="",
        )
    )
    if not body:
        return Recovery([])
    parsed = parse_diff(f"diff --git a/{old_path} b/{new_path}\n{body}\n")
    return Recovery(parsed[0].hunks if parsed else [])


def _gitlab_content(client: ForgeClient, target: Target, path: str, ref: str) -> bytes:
    encoded = quote(path, safe="")
    return client.get_raw(
        f"/projects/{target.encoded_project}/repository/files/{encoded}/raw", ref=ref
    )
