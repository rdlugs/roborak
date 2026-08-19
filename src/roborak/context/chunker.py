"""Split an oversized change across several model calls.

The compressor's job is to make a change fit by dropping things. This module's
job is to avoid dropping them: when a diff exceeds one context budget, review it
in several passes and merge the results, so a large pull request gets a complete
review rather than a partial one.

Files are kept whole and grouped by directory, because related files reviewed
together produce better findings than an arbitrary split down the middle.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import PurePosixPath

from roborak.core.models import ChangedFile, ChangeSet

log = logging.getLogger(__name__)

MAX_CHUNKS = 8
"""Beyond this the review costs more than it is worth; the rest is omitted."""


def needs_chunking(
    changeset: ChangeSet,
    budget: int,
    count_tokens: Callable[[str], int],
    render: Callable[[ChangedFile], str],
) -> bool:
    return count_tokens(_joined(changeset.files, render)) > budget


def chunk(
    changeset: ChangeSet,
    budget: int,
    count_tokens: Callable[[str], int],
    render: Callable[[ChangedFile], str],
) -> list[ChangeSet]:
    """Divide ``changeset`` into pieces that each fit ``budget``.

    Every chunk keeps the parent's metadata, so each pass sees the same title,
    description and forge refs and can produce properly anchored findings.
    """
    if not changeset.files:
        return []

    ordered = sorted(changeset.files, key=_grouping_key)
    chunks: list[list[ChangedFile]] = []
    current: list[ChangedFile] = []

    for file in ordered:
        # Measure the joined text rather than summing per-file costs: the joined
        # form is what the prompt actually carries, and summing rounds each file
        # down independently, which lets a chunk drift over budget.
        if current and count_tokens(_joined([*current, file], render)) > budget:
            chunks.append(current)
            current = []
        current.append(file)

    if current:
        chunks.append(current)

    omitted: list[str] = []
    if len(chunks) > MAX_CHUNKS:
        for extra in chunks[MAX_CHUNKS:]:
            omitted.extend(f.path for f in extra)
        chunks = chunks[:MAX_CHUNKS]
        log.warning(
            "change needs more than %d passes; %d file(s) omitted", MAX_CHUNKS, len(omitted)
        )

    log.debug("split %d files into %d chunk(s)", len(changeset.files), len(chunks))
    return [
        _sub_changeset(changeset, files, omitted if i == 0 else [])
        for i, files in enumerate(chunks)
    ]


def _joined(files: list[ChangedFile], render: Callable[[ChangedFile], str]) -> str:
    """Exactly how the prompt template joins a chunk's files."""
    return "\n".join(render(f) for f in files)


def _sub_changeset(parent: ChangeSet, files: list[ChangedFile], omitted: list[str]) -> ChangeSet:
    return ChangeSet(
        files=files,
        title=parent.title,
        description=parent.description,
        base_sha=parent.base_sha,
        head_sha=parent.head_sha,
        base_ref=parent.base_ref,
        head_ref=parent.head_ref,
        origin=parent.origin,
        forge_ref=parent.forge_ref,
        omitted_files=list(omitted),
    )


def _grouping_key(file: ChangedFile) -> tuple[str, str]:
    """Group by directory, then language, so a chunk holds related code."""
    return (str(PurePosixPath(file.path).parent), file.language or "")
