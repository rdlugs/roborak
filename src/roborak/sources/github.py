"""GitHub pull requests as a change source.

The ``files`` endpoint returns each file's patch already split out, which maps
cleanly onto ``ChangedFile``. Files with no ``patch`` are binary or too large for
GitHub to render, and are carried through as binary so nothing tries to review
them.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from roborak.context.diff import detect_language, parse_diff
from roborak.core.models import ChangedFile, ChangeSet, ForgeRef, Hunk
from roborak.sources.base import SourceError
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

    def load(self) -> ChangeSet:
        base = f"/repos/{self.target.project}/pulls/{self.target.number}"
        with ForgeClient(self.target, self.token) as client:
            pull = client.get(base)
            files = client.paginate(f"{base}/files")

        if not isinstance(pull, dict):
            raise SourceError("Unexpected pull request payload from GitHub.")

        base_sha = ((pull.get("base") or {}).get("sha")) or ""
        head_sha = ((pull.get("head") or {}).get("sha")) or ""

        return ChangeSet(
            files=[_to_changed_file(entry) for entry in files if isinstance(entry, dict)],
            title=pull.get("title"),
            description=pull.get("body"),
            base_sha=base_sha,
            head_sha=head_sha,
            base_ref=(pull.get("base") or {}).get("ref"),
            head_ref=(pull.get("head") or {}).get("ref"),
            origin="github",
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


def _to_changed_file(entry: dict[str, object]) -> ChangedFile:
    path = str(entry.get("filename") or "")
    patch = entry.get("patch")

    hunks: list[Hunk] = []
    if isinstance(patch, str) and patch:
        old = str(entry.get("previous_filename") or path)
        synthetic = f"diff --git a/{old} b/{path}\n--- a/{old}\n+++ b/{path}\n{patch}"
        parsed = parse_diff(synthetic)
        hunks = parsed[0].hunks if parsed else []

    previous = entry.get("previous_filename")
    return ChangedFile(
        path=path,
        previous_path=str(previous) if previous else None,
        change_type=_STATUS_MAP.get(str(entry.get("status") or ""), "modified"),  # type: ignore[arg-type]
        language=detect_language(path),
        hunks=hunks,
        # No patch means binary, or a diff GitHub declined to render.
        is_binary=not isinstance(patch, str) or not patch,
    )
