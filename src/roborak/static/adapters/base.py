"""The contract a static-analysis adapter implements.

Adapters are deliberately thin: locate the tool, decide which files it applies to,
build a read-only command, and map its output onto ``Finding``. Anything an
adapter cannot map, it drops -- a static tool's own severity vocabulary is never
allowed to leak into the report.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from roborak.core.models import ChangedFile, Finding


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
            if candidate.is_file() and candidate.stat().st_mode & 0o111:
                return str(candidate)
        return shutil.which(self.binary)

    def local_paths(self, repo: Path) -> list[Path]:
        return [
            repo / "node_modules" / ".bin" / self.binary,
            repo / "vendor" / "bin" / self.binary,
            repo / ".venv" / "bin" / self.binary,
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
