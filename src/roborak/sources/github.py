"""GitHub pull requests as a change source.

The ``files`` endpoint returns each file's patch already split out, which maps
cleanly onto ``ChangedFile``. Files with no ``patch`` are binary or too large for
GitHub to render, and are carried through as binary so nothing tries to review
them.
"""

from __future__ import annotations

import base64
import difflib
import logging
from dataclasses import dataclass
from urllib.parse import quote

from roborak.context.diff import detect_language, parse_diff
from roborak.core.models import ChangedFile, ChangeSet, ForgeRef, Hunk
from roborak.sources.base import SourceError
from roborak.sources.discussion import load_change_discussions
from roborak.sources.forge import ForgeClient, Target

log = logging.getLogger(__name__)

_STATUS_MAP = {
    "added": "added",
    "removed": "deleted",
    "modified": "modified",
    "renamed": "renamed",
    "copied": "added",
    "changed": "modified",
}


@dataclass
class GitHubSource:
    target: Target
    token: str
    max_recovered_file_bytes: int = 1_048_576
    include_discussions: bool = True

    def load(self) -> ChangeSet:
        base = f"/repos/{self.target.project}/pulls/{self.target.number}"
        with ForgeClient(self.target, self.token) as client:
            pull = client.get(base)
            entries = client.paginate(f"{base}/files")
            if not isinstance(pull, dict):
                raise SourceError("Unexpected pull request payload from GitHub.")

            base_sha = ((pull.get("base") or {}).get("sha")) or ""
            head_sha = ((pull.get("head") or {}).get("sha")) or ""
            files = [
                _to_changed_file(
                    entry,
                    client,
                    self.target,
                    base_sha,
                    head_sha,
                    self.max_recovered_file_bytes,
                )
                for entry in entries
                if isinstance(entry, dict)
            ]
            discussions = (
                load_change_discussions(client, self.target, head_sha=head_sha)
                if self.include_discussions
                else []
            )

        return ChangeSet(
            files=files,
            title=pull.get("title"),
            description=pull.get("body"),
            base_sha=base_sha,
            head_sha=head_sha,
            base_ref=(pull.get("base") or {}).get("ref"),
            head_ref=(pull.get("head") or {}).get("ref"),
            origin="github",
            discussions=discussions,
            forge_ref=ForgeRef(
                provider="github",
                host=self.target.host,
                project=self.target.project,
                number=self.target.number,
                base_sha=base_sha,
                head_sha=head_sha,
                web_url=pull.get("html_url"),
            ),
        )


def _to_changed_file(
    entry: dict[str, object],
    client: ForgeClient | None = None,
    target: Target | None = None,
    base_sha: str = "",
    head_sha: str = "",
    max_bytes: int = 1_048_576,
) -> ChangedFile:
    path = str(entry.get("filename") or "")
    patch = entry.get("patch")

    hunks: list[Hunk] = []
    if isinstance(patch, str) and patch:
        old = str(entry.get("previous_filename") or path)
        synthetic = f"diff --git a/{old} b/{path}\n--- a/{old}\n+++ b/{path}\n{patch}"
        parsed = parse_diff(synthetic)
        hunks = parsed[0].hunks if parsed else []

    previous = entry.get("previous_filename")
    is_binary = False
    patch_unavailable = False
    if not hunks and _known_binary_path(path):
        is_binary = True
    elif not hunks and client is not None and target is not None:
        try:
            recovered = _recover_patch(
                client,
                target,
                path,
                str(previous or path),
                str(entry.get("status") or "modified"),
                base_sha,
                head_sha,
                max_bytes,
            )
            if recovered is None:
                is_binary = True
            else:
                hunks = recovered
                patch_unavailable = not hunks and str(entry.get("status")) != "removed"
        except SourceError as exc:
            log.warning("could not recover GitHub patch for %s: %s", path, exc)
            patch_unavailable = True
    elif not hunks:
        is_binary = True
    return ChangedFile(
        path=path,
        previous_path=str(previous) if previous else None,
        change_type=_STATUS_MAP.get(str(entry.get("status") or ""), "modified"),  # type: ignore[arg-type]
        language=detect_language(path),
        hunks=hunks,
        is_binary=is_binary,
        patch_unavailable=patch_unavailable,
    )


def _recover_patch(
    client: ForgeClient,
    target: Target,
    new_path: str,
    old_path: str,
    status: str,
    base_sha: str,
    head_sha: str,
    max_bytes: int,
) -> list[Hunk] | None:
    old = b"" if status == "added" else _github_content(client, target, old_path, base_sha)
    new = b"" if status == "removed" else _github_content(client, target, new_path, head_sha)
    if len(old) > max_bytes or len(new) > max_bytes:
        raise SourceError(f"file exceeds recovery limit of {max_bytes} bytes")
    if b"\0" in old or b"\0" in new:
        return None
    try:
        old_text, new_text = old.decode(), new.decode()
    except UnicodeDecodeError:
        return None
    return _hunks_from_contents(old_path, new_path, old_text, new_text)


def _github_content(client: ForgeClient, target: Target, path: str, ref: str) -> bytes:
    data = client.get(f"/repos/{target.project}/contents/{quote(path, safe='/')}", ref=ref)
    if not isinstance(data, dict) or data.get("encoding") != "base64":
        raise SourceError(f"GitHub did not return base64 content for {path}")
    try:
        return base64.b64decode(str(data.get("content") or ""), validate=False)
    except ValueError as exc:
        raise SourceError(f"GitHub returned invalid content for {path}") from exc


def _hunks_from_contents(old_path: str, new_path: str, old: str, new: str) -> list[Hunk]:
    body = "\n".join(
        difflib.unified_diff(
            old.splitlines(),
            new.splitlines(),
            fromfile=f"a/{old_path}",
            tofile=f"b/{new_path}",
            lineterm="",
        )
    )
    if not body:
        return []
    parsed = parse_diff(f"diff --git a/{old_path} b/{new_path}\n{body}\n")
    return parsed[0].hunks if parsed else []


def _known_binary_path(path: str) -> bool:
    return path.lower().endswith(
        (".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf", ".zip", ".gz")
    )
