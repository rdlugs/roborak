"""The contract a static-analysis adapter implements.

Adapters are deliberately thin: locate the tool, decide which files it applies to,
build a read-only command, and map its output onto ``Finding``. Anything an
adapter cannot map, it drops -- a static tool's own severity vocabulary is never
allowed to leak into the report.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from roborak.context.compressor import matches_any
from roborak.core.models import ChangedFile, Finding

_WINDOWS = os.name == "nt"


@dataclass
class ToolRun:
    """What an adapter asks the runner to execute."""

    command: list[str]
    files: list[str]


class Adapter:
    name: str = ""
    binary: str = ""
    languages: frozenset[str] = frozenset()
    """Languages this tool understands. Empty means "any"."""

    paths: tuple[str, ...] = ()
    """Ignore-style globs this tool applies to, when a language is too coarse.

    A GitHub workflow's language is ``yaml``, which it shares with every
    configuration file in the repository, so a language filter would run a
    workflow linter over all of them. When this is set it replaces the language
    filter rather than narrowing it: the tool knows its own files by path."""

    requires_network: bool = False
    """Whether running this tool reaches the network.

    Never autodetected. The static pass promises it installs nothing and fetches
    nothing, and a tool that queries a vulnerability service breaks the second
    half of that promise however useful it is -- so it runs only when a project
    names it in ``static.tools``, which is a person choosing, not a default."""

    report_only: bool = False
    """The finding describes a whole asset and has no meaningful line anchor."""

    def find_binary(self, repo: Path) -> str | None:
        """Locate the tool, preferring a project-local install over a global one.

        A repo's pinned version is the one whose findings the team already agrees
        with, so it wins over whatever happens to be on PATH.
        """
        for candidate in self.local_paths(repo):
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)
        return shutil.which(self.binary)

    def local_paths(self, repo: Path) -> list[Path]:
        """Every place a project-local copy of the tool might sit, best first.

        Windows keeps virtualenv entry points in ``Scripts`` rather than ``bin``,
        and reaches them through a suffix from ``PATHEXT`` -- npm ships ``.cmd``
        shims, uv ships ``.exe``. A bare name matches nothing there.
        """
        directories = [
            repo / "node_modules" / ".bin",
            repo / "vendor" / "bin",
            repo / ".venv" / ("Scripts" if _WINDOWS else "bin"),
        ]
        # The empty suffix first keeps POSIX lookup exactly as it was.
        suffixes = ("", ".exe", ".cmd", ".bat") if _WINDOWS else ("",)
        return [
            directory / f"{self.binary}{suffix}" for directory in directories for suffix in suffixes
        ]

    def applicable(self, files: list[ChangedFile]) -> list[ChangedFile]:
        if self.paths:
            return [f for f in files if matches_any(f.path, self.paths)]
        if not self.languages:
            return files
        return [f for f in files if f.language in self.languages]

    def is_available(self, repo: Path, files: list[ChangedFile]) -> bool:
        return bool(self.applicable(files)) and self.find_binary(repo) is not None

    def build(self, executable: str, files: list[str], repo: Path) -> ToolRun:
        raise NotImplementedError

    def parse(self, stdout: str, stderr: str, returncode: int) -> list[Finding]:
        raise NotImplementedError
