"""The execution boundary for investigation requests.

Everything a model may ask of the repository lives here, and nothing here knows
what a model is. The rules are the same three the rest of roborak already
applies to untrusted input, restated for a stage that takes its instructions
from the model rather than from configuration:

- a path is resolved and proved to be inside the repository before any I/O, so
  ``..`` and a symlink pointing out of the tree are the same rejection;
- a search runs as argv, never through a shell, and never with the caller's
  credentials in its environment;
- every result is bounded and says so when it was cut, because a silently
  truncated tail reads as "there is nothing else there".

No operation writes, applies a patch, reaches the network, or runs a command the
repository chose. The read side is deliberately the whole surface.
"""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from roborak.core.config import InvestigateConfig
from roborak.core.models import ChangeSet
from roborak.sandbox import safe_environment
from roborak.sources.paths import MAX_FILE_BYTES

log = logging.getLogger(__name__)

VCS_METADATA = frozenset({".git", ".hg", ".svn"})
"""Directories that live inside the tree without being part of it."""

MAX_PATTERN_CHARS = 200
"""Ceiling on a search pattern. A regular expression long enough to exceed this is
not a question about the change."""


@dataclass
class ToolResult:
    """What one operation produced, before it is recorded on the report."""

    text: str = ""
    truncated: bool = False
    error: str = ""
    """Non-empty means the operation could not answer. The caller records it and
    leaves the candidate unverified rather than reading an empty result as "no"."""

    @property
    def ok(self) -> bool:
        return not self.error


def resolve_in_repo(repo: Path, candidate: str) -> Path | None:
    """The requested path as a real path inside ``repo``, or ``None``.

    ``resolve`` follows symlinks, so a link pointing out of the tree and a literal
    ``../`` fail the same containment check rather than needing separate tests. An
    absolute path is rejected outright: the model is asking about a repository, and
    a path that does not start relative to one is not a question about the change.
    Version-control metadata is rejected on the same grounds: it sits inside the
    tree without being part of the change.
    """
    if not candidate or candidate.strip() != candidate:
        return None
    if Path(candidate).is_absolute() or "\x00" in candidate:
        return None
    try:
        root = repo.resolve(strict=False)
        target = (root / candidate).resolve(strict=False)
    except (OSError, ValueError, RuntimeError):
        # RuntimeError is a symlink loop, which is an escape attempt or a broken
        # tree; either way it is not a path we are willing to open.
        return None
    if target != root and not target.is_relative_to(root):
        log.debug("refusing path outside the repository: %s", candidate)
        return None
    if VCS_METADATA.intersection(target.relative_to(root).parts):
        # Inside the repository but not part of it: `.git` holds the object store,
        # hooks and remote configuration, none of which is a question about the change.
        log.debug("refusing version-control metadata path: %s", candidate)
        return None
    return target


def bound(text: str, limit: int) -> tuple[str, bool]:
    """Cut to ``limit`` characters, saying whether anything was lost."""
    if len(text) <= limit:
        return text, False
    return text[:limit], True


def read_lines(
    repo: Path,
    path: str,
    *,
    start: int,
    end: int,
    config: InvestigateConfig,
) -> ToolResult:
    """A bounded line range from a repository file, numbered as the file reads.

    Numbered because every line the model may go on to cite is a new-file
    coordinate, and handing it an unnumbered block invites it to count.
    """
    target = resolve_in_repo(repo, path)
    if target is None:
        return ToolResult(error=f"path is outside the repository or not part of it: {path}")
    if not target.is_file():
        return ToolResult(error=f"not a file: {path}")
    try:
        if target.stat().st_size > MAX_FILE_BYTES:
            return ToolResult(error=f"file is too large to read: {path}")
        content = target.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return ToolResult(error=f"could not read {path}: {exc}")

    lines = content.splitlines()
    first = max(1, start)
    last = min(len(lines), max(first, end))
    if first > len(lines):
        return ToolResult(error=f"{path} has {len(lines)} lines; {first} is past the end")

    window = lines[first - 1 : last]
    clipped = len(window) > config.max_lines_per_read
    window = window[: config.max_lines_per_read]
    numbered = "\n".join(f"{first + offset}: {line}" for offset, line in enumerate(window))
    text, cut = bound(numbered, config.max_output_chars)
    return ToolResult(text=text, truncated=clipped or cut)


IDENTIFIER = re.compile(r"^\w+$")


def search(
    repo: Path,
    pattern: str,
    *,
    regex: bool,
    path_prefix: str,
    config: InvestigateConfig,
) -> ToolResult:
    """``git grep`` as argv, bounded by count and by output size.

    Modelled on the blast-radius search rather than sharing it: that one is fixed
    strings across the whole tree on a deadline shared by every term, and this one
    needs a regular-expression mode, a result ceiling, and a path restriction. What
    is shared is the error discipline -- a return code above 1 is a broken search
    and not an empty one, and an empty search is an answer.
    """
    if not pattern or len(pattern) > MAX_PATTERN_CHARS:
        return ToolResult(error="search pattern is empty or too long")
    if regex:
        try:
            re.compile(pattern)
        except re.error as exc:
            return ToolResult(error=f"invalid regular expression: {exc}")

    args = [
        "grep",
        "-n",
        "-I",
        "--untracked",
        "--exclude-standard",
        "-E" if regex else "--fixed-strings",
        f"--max-count={config.max_search_results}",
    ]
    if not regex and IDENTIFIER.match(pattern):
        # `-w` is what stops `run` matching `rerun`. Only for identifiers: a regular
        # expression already says where it wants to anchor.
        args.append("-w")

    argv = ["git", *args, "-e", pattern, "--"]
    if path_prefix:
        contained = resolve_in_repo(repo, path_prefix)
        if contained is None:
            return ToolResult(
                error=f"path is outside the repository or not part of it: {path_prefix}"
            )
        # Pass the path as the model wrote it, not as we resolved it: git wants a
        # repo-relative pathspec, and the resolution above was the containment proof.
        argv.append(path_prefix)

    try:
        done = subprocess.run(
            argv,
            cwd=repo,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=safe_environment(),
            timeout=config.timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return ToolResult(error="search timed out")
    except OSError as exc:
        return ToolResult(error=f"search could not run: {exc}")

    if done.returncode not in (0, 1):  # 1 is "no matches", which is an answer
        # Not `> 1`: a git killed by a signal comes back negative, and reading that
        # as success would hand the model an empty result as if it were evidence.
        # 128 is usually a pattern git itself rejected, and saying so is the
        # difference between a question the model can rephrase and one it cannot.
        detail = next((line for line in done.stderr.splitlines() if line.strip()), "")
        if done.returncode == 128 and detail:
            return ToolResult(error=f"search failed: {detail.strip()}")
        return ToolResult(error="search failed; the checkout may not be a git repository")

    rows = done.stdout.splitlines()
    clipped = len(rows) > config.max_search_results
    text, cut = bound("\n".join(rows[: config.max_search_results]), config.max_output_chars)
    return ToolResult(text=text, truncated=clipped or cut)


def show_diff(changeset: ChangeSet, path: str, *, config: InvestigateConfig) -> ToolResult:
    """The reviewed diff for one file, straight from the changeset.

    Served from memory rather than from git: the changeset is what is under
    review, and a working tree that has moved on would answer a different question
    from the one asked.
    """
    file = changeset.file_by_path(path)
    if file is None:
        known = ", ".join(sorted(f.path for f in changeset.files)[:20])
        return ToolResult(error=f"{path} is not in this change. Changed files: {known}")
    body = "\n".join(hunk.header + "\n" + hunk.content for hunk in file.hunks)
    if not body.strip():
        return ToolResult(error=f"no diff text is available for {path}")
    text, cut = bound(body, config.max_output_chars)
    return ToolResult(text=text, truncated=cut)
