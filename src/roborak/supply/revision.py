"""Read a file as of a git revision.

The analysis needs both sides of a manifest and its lockfile, and it cannot get
them from the diff: a lockfile's hunks are a line-level view of generated data,
where a reformat or a moved block looks like a hundred package changes. Parsing
both whole files and diffing the results is the only way to get an answer that
means what it says.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

MAX_BYTES = 8 * 1024 * 1024
"""Largest file read from a revision. A lockfile above this is a generated tree
big enough that parsing it would cost more than the review it informs."""


def read_at(repo: Path, ref: str, path: str, *, timeout: int = 10) -> str | None:
    """The contents of ``path`` at ``ref``, or ``None`` when it cannot be read.

    ``None`` covers a revision that is not there, a path that did not exist at it,
    a directory that is not a git repository, and a repository with no commits.
    All four mean the same thing here -- there is no readable copy -- and none of
    them is an error, so each becomes a note rather than a failure.
    """
    if not ref:
        return None
    try:
        shown = subprocess.run(
            ["git", "show", f"{ref}:{path}"],
            cwd=repo,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.debug("could not read %s at %s: %s", path, ref, exc)
        return None
    if shown.returncode != 0:
        return None
    return shown.stdout[:MAX_BYTES]


def read_working_tree(repo: Path, path: str) -> str | None:
    """The file as it stands on disk, for the head side of a local review.

    The working tree is the head side of an uncommitted change, which is the
    common case for ``rk review`` and the one a committed-only read would miss.
    """
    target = repo / path
    try:
        if not target.is_file() or target.stat().st_size > MAX_BYTES:
            return None
        return target.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        log.debug("could not read %s from the working tree: %s", path, exc)
        return None


def base_revision(repo: Path, base: str, *, timeout: int = 10) -> str:
    """The revision the diff was actually taken against.

    Not simply ``base_sha``. A local review diffs against ``git merge-base <base>
    HEAD``, so reading the old side from the base branch's tip would compare this
    change against every commit that landed on that branch since it was cut --
    reporting a dependency somebody else bumped as part of this change.

    Falls back to ``HEAD``, which is the base a diff with no configured base ref
    uses, and is the right answer for an uncommitted change.
    """
    if not base:
        return "HEAD"
    try:
        merged = subprocess.run(
            ["git", "merge-base", base, "HEAD"],
            cwd=repo,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return base
    resolved = merged.stdout.strip()
    return resolved if merged.returncode == 0 and resolved else base
