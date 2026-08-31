"""Fit a changeset into the model's context window, predictably.

The rule this module exists to enforce: never silently review half a diff. When
the change does not fit, we degrade in a defined order and record every omission
in ``ChangeSet.omitted_files`` so the report can say what was skipped.

Degradation order (cheapest loss first):
1. Drop files matching ``ignore_paths`` and binaries -- no information lost.
2. Drop the bodies of deleted files -- the fact of deletion is what matters.
3. Trim surplus context out of oversized hunks (done by the renderer, which
   still has correct line numbers in hand).
4. Omit whole files, least interesting first.
"""

from __future__ import annotations

import fnmatch
import logging
from collections.abc import Callable, Sequence

from roborak.core.models import ChangedFile, ChangeSet

log = logging.getLogger(__name__)

_LANGUAGE_PRIORITY: dict[str | None, int] = {
    "python": 10,
    "typescript": 10,
    "javascript": 10,
    "go": 10,
    "rust": 10,
    "php": 10,
    "java": 10,
    "ruby": 10,
    "csharp": 10,
    "kotlin": 10,
    "swift": 10,
    "c": 9,
    "cpp": 9,
    "scala": 9,
    "sql": 8,
    "terraform": 8,
    "shell": 7,
    "vue": 9,
    "html": 4,
    "css": 3,
    "scss": 3,
    "yaml": 5,
    # A manifest is small, and what it declares -- a dependency, a permission, a
    # base image -- outweighs most code changes it travels with. Ranked above
    # plain data because dropping `package.json` under budget pressure while
    # keeping a stylesheet is exactly backwards.
    "dockerfile": 8,
    "toml": 6,
    "json": 4,
    "make": 4,
    "groovy": 4,
    "markdown": 1,
    None: 5,
}

MAX_HUNK_LINES = 400
"""Longest hunk shown in full. Beyond this the renderer drops surplus context."""


def filter_files(changeset: ChangeSet, ignore_paths: list[str]) -> ChangeSet:
    """Remove ignored, binary, and empty-diff files. Records nothing: these are noise."""
    kept: list[ChangedFile] = []
    for file in changeset.files:
        if file.is_binary:
            continue
        if matches_any(file.path, ignore_paths):
            log.debug("ignoring %s (matches ignore_paths)", file.path)
            continue
        if not file.hunks and file.new_content is None:
            continue
        kept.append(file)
    changeset.files = kept
    return changeset


def compress(
    changeset: ChangeSet,
    budget_tokens: int,
    count_tokens: Callable[[str], int],
    *,
    render: Callable[[ChangedFile], str],
) -> ChangeSet:
    """Shrink ``changeset`` until its rendered diff fits ``budget_tokens``."""
    if not changeset.files:
        return changeset

    for file in changeset.files:
        if file.change_type == "deleted":
            file.hunks = []
            file.new_content = None

    if _fits(changeset, budget_tokens, count_tokens, render):
        return changeset

    ordered = sorted(changeset.files, key=_interest, reverse=True)
    kept: list[ChangedFile] = []
    omitted: list[str] = []

    for file in ordered:
        trial = [*kept, file]
        rendered = "\n".join(render(f) for f in trial)
        if count_tokens(rendered) <= budget_tokens:
            kept.append(file)
        else:
            omitted.append(file.path)

    if omitted:
        log.warning("omitted %d files to fit the context budget", len(omitted))

    order = {f.path: i for i, f in enumerate(changeset.files)}
    changeset.files = sorted(kept, key=lambda f: order[f.path])
    changeset.omitted_files.extend(sorted(omitted))
    return changeset


def _fits(
    changeset: ChangeSet,
    budget: int,
    count_tokens: Callable[[str], int],
    render: Callable[[ChangedFile], str],
) -> bool:
    rendered = "\n".join(render(f) for f in changeset.files)
    return count_tokens(rendered) <= budget


def _interest(file: ChangedFile) -> tuple[int, int]:
    """Rank files for retention: language priority first, then amount changed."""
    return (_LANGUAGE_PRIORITY.get(file.language, 5), len(file.added_lines))


def matches_any(path: str, patterns: Sequence[str]) -> bool:
    """Whether a repo-relative path matches any ignore-style glob."""
    for pattern in patterns:
        if fnmatch.fnmatch(path, pattern):
            return True
        if pattern.startswith("**/") and fnmatch.fnmatch(path, pattern[3:]):
            return True
    return False
