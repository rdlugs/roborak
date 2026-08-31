"""A directory on disk as a change source.

Nothing here is a diff: every eligible file is reviewed whole, as though it had
just been added. That is what makes reviewing an extracted archive, a generated
tree or a non-git checkout possible at all -- there is no baseline to compare
against, and inventing one with ``git init`` would mutate the thing under review.

The walk is deliberately conservative. It prunes the directories nobody wants
reviewed before descending into them (the difference between reading a project
and reading its ``node_modules``), and it reports what it declined to read as an
omission rather than dropping it silently.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from roborak.context.compressor import matches_any
from roborak.context.diff import detect_language, whole_file_hunk
from roborak.core.models import ChangedFile, ChangeSet
from roborak.sources.base import SourceError

log = logging.getLogger(__name__)

SKIP_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".bzr",
        "node_modules",
        "vendor",
        "dist",
        "build",
        "target",
        "out",
        "__pycache__",
        ".venv",
        "venv",
        ".tox",
        ".nox",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".cache",
        ".next",
        ".nuxt",
        ".gradle",
        ".idea",
        ".terraform",
    }
)
"""Dependency, build-output, cache and VCS directories, never descended into."""

MAX_FILE_BYTES = 512 * 1024
"""Beyond this a single file is more context than a review can spend on it."""

MAX_FILES = 2000
"""A ceiling on the walk, so a vast tree reports what it left out instead of hanging."""

_BINARY_SNIFF_BYTES = 8192


@dataclass
class PathsSource:
    """Every eligible file beneath ``root``, reviewed as a whole file."""

    root: Path
    ignore_paths: list[str] = field(default_factory=list)
    max_file_bytes: int = MAX_FILE_BYTES
    max_files: int = MAX_FILES
    keep: Callable[[str], bool] | None = None
    """Paths that survive ``ignore_paths``.

    A lockfile is ignored for the prompt and still needed by the supply-chain
    stage, so it is kept here and dropped later, rather than the whole of
    ``ignore_paths`` being skipped to save it."""

    def load(self) -> ChangeSet:
        self._ensure_directory()
        changeset = ChangeSet(
            files=[],
            origin="paths",
            title=f"Whole-file review of {self.root.name or self.root}",
        )

        for relative in self._eligible_paths(changeset):
            file = self._read(relative, changeset)
            if file is not None:
                changeset.files.append(file)
        return changeset

    def _ensure_directory(self) -> None:
        if not self.root.exists():
            raise SourceError(f"{self.root} does not exist.")
        if not self.root.is_dir():
            raise SourceError(f"{self.root} is not a directory.")
        if not os.access(self.root, os.R_OK | os.X_OK):
            raise SourceError(f"{self.root} is not readable.")

    def _eligible_paths(self, changeset: ChangeSet) -> list[str]:
        """Repo-relative paths worth reading, sorted so runs are reproducible.

        Anything past ``max_files`` is recorded rather than dropped: a truncated
        review that says so is honest, one that does not is a lie about coverage.
        """

        def raise_walk_error(error: OSError) -> None:
            raise SourceError(f"Could not scan {error.filename or self.root}: {error}") from error

        found: list[str] = []
        for dirpath, dirnames, filenames in os.walk(
            self.root, followlinks=False, onerror=raise_walk_error
        ):
            here = Path(dirpath)
            dirnames[:] = sorted(
                name
                for name in dirnames
                if name not in SKIP_DIRS and not (here / name).is_symlink()
            )
            for name in sorted(filenames):
                path = here / name
                if path.is_symlink() or not path.is_file():
                    continue
                relative = path.relative_to(self.root).as_posix()
                if matches_any(relative, self.ignore_paths) and not (
                    self.keep is not None and self.keep(relative)
                ):
                    log.debug("ignoring %s (matches ignore_paths)", relative)
                    continue
                found.append(relative)

        found.sort()
        if len(found) > self.max_files:
            log.warning("%d files found; reviewing the first %d", len(found), self.max_files)
            changeset.omitted_files.extend(found[self.max_files :])
            found = found[: self.max_files]
        return found

    def _read(self, relative: str, changeset: ChangeSet) -> ChangedFile | None:
        """One file as a whole-file addition, or a marker for why it is not reviewable."""
        path = self.root / relative
        try:
            size = path.stat().st_size
        except OSError as exc:
            log.debug("skipping %s: %s", relative, exc)
            changeset.omitted_files.append(relative)
            return None

        if size > self.max_file_bytes:
            log.debug("skipping %s: %d bytes exceeds the per-file limit", relative, size)
            changeset.omitted_files.append(relative)
            return None

        try:
            raw = path.read_bytes()
        except OSError as exc:
            log.debug("skipping %s: %s", relative, exc)
            changeset.omitted_files.append(relative)
            return None

        if b"\x00" in raw[:_BINARY_SNIFF_BYTES]:
            return _binary(relative)
        try:
            content = _decode(raw)
        except UnicodeDecodeError:
            return _binary(relative)

        return ChangedFile(
            path=relative,
            change_type="added",
            language=detect_language(relative),
            new_content=content,
            hunks=whole_file_hunk(content),
        )


def _decode(raw: bytes) -> str:
    """UTF-8 text with newlines normalised, the way every other source sees a file.

    Reading bytes is what lets us sniff for binaries, but it also opts out of the
    universal-newline translation ``read_text`` performs -- so a CRLF checkout
    reviewed on Windows would carry a literal carriage return into every prompt,
    every ``new_content`` and every rendered line, unlike the same file read
    through ``LocalGitSource``. One IR means one spelling of a line ending.
    """
    return raw.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")


def _binary(relative: str) -> ChangedFile:
    """A file the review cannot read, carried through so it is reported as omitted."""
    return ChangedFile(path=relative, change_type="added", is_binary=True)
