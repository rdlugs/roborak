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

from roborak.core.models import ChangedFile, ChangeSet, Hunk

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

    ordered = [
        fragment
        for file in sorted(changeset.files, key=_grouping_key)
        for fragment in _fragments(file, budget, count_tokens, render)
    ]
    chunks: list[list[ChangedFile]] = []
    current: list[ChangedFile] = []

    for file in ordered:
        if current and count_tokens(_joined([*current, file], render)) > budget:
            chunks.append(current)
            current = []
        current.append(file)

    if current:
        chunks.append(current)

    omitted: list[str] = []
    if len(chunks) > MAX_CHUNKS:
        for extra in chunks[MAX_CHUNKS:]:
            omitted.extend(f.path for f in extra if f.path not in omitted)
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


def _fragments(
    file: ChangedFile,
    budget: int,
    count_tokens: Callable[[str], int],
    render: Callable[[ChangedFile], str],
) -> list[ChangedFile]:
    """Split a lone oversized file by hunk and then by line windows."""
    if count_tokens(render(file)) <= budget or not file.hunks:
        return [file]

    fragments: list[ChangedFile] = []
    for hunk in file.hunks:
        fragments.extend(_hunk_fragments(file, hunk, budget, count_tokens, render))
    return fragments or [file]


def _hunk_fragments(
    file: ChangedFile,
    hunk: Hunk,
    budget: int,
    count_tokens: Callable[[str], int],
    render: Callable[[ChangedFile], str],
) -> list[ChangedFile]:
    candidate = file.model_copy(update={"hunks": [hunk]})
    lines = hunk.content.splitlines()
    if count_tokens(render(candidate)) <= budget or len(lines) <= 1:
        return [candidate]

    midpoint = len(lines) // 2
    overlap = min(3, max(0, len(lines) // 4))
    left = _slice_hunk(hunk, 0, midpoint + overlap)
    right = _slice_hunk(hunk, midpoint - overlap, len(lines))
    return [
        *_hunk_fragments(file, left, budget, count_tokens, render),
        *_hunk_fragments(file, right, budget, count_tokens, render),
    ]


def _slice_hunk(hunk: Hunk, start: int, end: int) -> Hunk:
    lines = hunk.content.splitlines()
    old_line, new_line = hunk.old_start, hunk.new_start
    for line in lines[:start]:
        if line.startswith("\\"):
            continue
        if not line.startswith("+"):
            old_line += 1
        if not line.startswith("-"):
            new_line += 1

    selected = lines[start:end]
    old_count = sum(1 for line in selected if not line.startswith(("+", "\\")))
    new_count = sum(1 for line in selected if not line.startswith(("-", "\\")))
    new_end = new_line + new_count
    return Hunk(
        old_start=old_line,
        old_lines=old_count,
        new_start=new_line,
        new_lines=new_count,
        header=f"@@ -{old_line},{old_count} +{new_line},{new_count} @@",
        content="\n".join(selected),
        added_lines={line for line in hunk.added_lines if new_line <= line < new_end},
        line_map={
            line: position for line, position in hunk.line_map.items() if new_line <= line < new_end
        },
    )
