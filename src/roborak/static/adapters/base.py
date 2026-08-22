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
        if not self.languages:
            return files
        return [f for f in files if f.language in self.languages]

    def is_available(self, repo: Path, files: list[ChangedFile]) -> bool:
        return bool(self.applicable(files)) and self.find_binary(repo) is not None

    def build(self, executable: str, files: list[str], repo: Path) -> ToolRun:
        raise NotImplementedError

    def parse(self, stdout: str, stderr: str, returncode: int) -> list[Finding]:
        raise NotImplementedError
