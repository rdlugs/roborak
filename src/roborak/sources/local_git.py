"""Local git as a change source.

Covers four scopes: everything tracked (the default), committed only,
uncommitted only, and a branch compared against a base.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from roborak.context.diff import detect_language, parse_diff
from roborak.core.models import ChangedFile, ChangeSet, Hunk
from roborak.sources.base import SourceError


class Scope(StrEnum):
    ALL = "all"
    """Committed changes vs base, plus staged, plus unstaged edits to tracked files."""

    COMMITTED = "committed"
    UNCOMMITTED = "uncommitted"
    """Staged and unstaged edits to tracked files, nothing already committed."""


@dataclass
class LocalGitSource:
    repo: Path
    scope: Scope = Scope.ALL
    base: str | None = None
    include_untracked: bool = False

    def load(self) -> ChangeSet:
        self._ensure_repo()
        base_ref = self.base or self._default_base()
        diff_text = self._collect_diff(base_ref)
        files = parse_diff(diff_text)

        if self.include_untracked:
            files.extend(self._untracked_files())

        changeset = ChangeSet(
            files=files,
            origin="local",
            base_ref=base_ref,
            head_ref=self._current_branch(),
            base_sha=self._rev_parse(base_ref) if base_ref else "",
            head_sha=self._rev_parse("HEAD"),
            title=self._head_subject(),
        )
        self._attach_contents(changeset)
        return changeset

    def _collect_diff(self, base_ref: str | None) -> str:
        if self.scope is Scope.UNCOMMITTED:
            return self._git("diff", "HEAD")
        if self.scope is Scope.COMMITTED:
            if not base_ref:
                raise SourceError("--committed needs a base; pass --base <ref>.")
            return self._git("diff", f"{base_ref}...HEAD")
        if base_ref:
            merge_base = self._git("merge-base", base_ref, "HEAD").strip() or base_ref
            return self._git("diff", merge_base)
        return self._git("diff", "HEAD")

    def _untracked_files(self) -> list[ChangedFile]:
        """Synthesise added-file entries for untracked paths.

        ``git diff`` never reports these, so we build the equivalent of a
        whole-file addition and let the normal pipeline handle it.
        """
        listed = self._git("ls-files", "--others", "--exclude-standard").split("\n")
        out: list[ChangedFile] = []
        for rel in filter(None, (line.strip() for line in listed)):
            path = self.repo / rel
            try:
                content = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            out.append(
                ChangedFile(
                    path=rel,
                    change_type="added",
                    language=detect_language(rel),
                    new_content=content,
                    hunks=_synthetic_hunk(content),
                )
            )
        return out

    def _attach_contents(self, changeset: ChangeSet) -> None:
        """Populate ``new_content`` from the working tree for AST context."""
        for file in changeset.files:
            if file.new_content is not None or file.is_binary or file.change_type == "deleted":
                continue
            path = self.repo / file.path
            try:
                file.new_content = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                file.new_content = None

    def _default_base(self) -> str | None:
        """Best guess at the branch this work will merge into."""
        if self.scope is Scope.UNCOMMITTED:
            return None
        for candidate in self._base_candidates():
            if self._ref_exists(candidate):
                return candidate
        return None

    def _base_candidates(self) -> list[str]:
        head = self._current_branch()
        candidates = []
        remote_head = self._git_quiet("symbolic-ref", "refs/remotes/origin/HEAD").strip()
        if remote_head:
            candidates.append(remote_head.removeprefix("refs/remotes/"))
        candidates += ["origin/main", "origin/master", "main", "master", "develop"]
        return [c for c in candidates if c != head]

    def _ref_exists(self, ref: str) -> bool:
        return self._run("git", "rev-parse", "--verify", "--quiet", ref).returncode == 0

    def _current_branch(self) -> str:
        return self._git_quiet("rev-parse", "--abbrev-ref", "HEAD").strip() or "HEAD"

    def _rev_parse(self, ref: str) -> str:
        return self._git_quiet("rev-parse", ref).strip()

    def _head_subject(self) -> str | None:
        return self._git_quiet("log", "-1", "--pretty=%s").strip() or None

    def _ensure_repo(self) -> None:
        if self._run("git", "rev-parse", "--git-dir").returncode != 0:
            raise SourceError(f"{self.repo} is not a git repository.")

    def _git(self, *args: str) -> str:
        result = self._run("git", *args)
        if result.returncode != 0:
            raise SourceError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
        return result.stdout

    def _git_quiet(self, *args: str) -> str:
        """Run git, returning empty output instead of raising. For best-effort probes."""
        return self._run("git", *args).stdout

    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            args,
            cwd=self.repo,
            capture_output=True,
            text=True,
            check=False,
        )


def _synthetic_hunk(content: str) -> list[Hunk]:
    """Build the hunk an untracked file *would* have if git had diffed it."""
    lines = content.splitlines()
    if not lines:
        return []
    hunk = Hunk(
        old_start=0,
        old_lines=0,
        new_start=1,
        new_lines=len(lines),
        content="\n".join(f"+{line}" for line in lines),
    )
    for index, _ in enumerate(lines, start=1):
        hunk.line_map[index] = index + 1
        hunk.added_lines.add(index)
    return [hunk]
