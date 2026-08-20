"""Run whichever static analysers this repo actually has.

Design rules:
- Never install anything, never reach the network, never modify the repo.
- Only analyse files the change touched; a whole-repo run would drown the review
  in pre-existing findings the author is not responsible for.
- A tool that crashes, hangs, or emits nonsense is skipped with a warning, never
  fatal. Static analysis is an enhancement to the review, not a precondition.
"""

from __future__ import annotations

import contextlib
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from roborak.core.config import StaticConfig, StaticExecution
from roborak.core.models import ChangedFile, ChangeSet, Finding
from roborak.static.adapters.base import Adapter
from roborak.static.adapters.eslint import EslintAdapter
from roborak.static.adapters.mypy import MypyAdapter
from roborak.static.adapters.phpstan import PhpstanAdapter
from roborak.static.adapters.ruff import RuffAdapter
from roborak.static.adapters.semgrep import SemgrepAdapter

log = logging.getLogger(__name__)

ALL_ADAPTERS: list[Adapter] = [
    RuffAdapter(),
    MypyAdapter(),
    SemgrepAdapter(),
    EslintAdapter(),
    PhpstanAdapter(),
]


@dataclass
class StaticRunner:
    repo: Path
    config: StaticConfig
    adapters: list[Adapter] = field(default_factory=lambda: list(ALL_ADAPTERS))

    def run(self, changeset: ChangeSet) -> list[Finding]:
        if not self.config.enabled or self.config.execution is StaticExecution.OFF:
            return []

        sandboxed = self._sandboxed_command()
        if self.config.execution is StaticExecution.AUTO and _in_ci() and sandboxed is None:
            log.warning(
                "static analysis skipped: CI checkout is untrusted and bubblewrap is unavailable; "
                "use --trust-static only when the checkout and tool configuration are trusted"
            )
            return []

        candidates = [f for f in changeset.files if f.change_type != "deleted" and not f.is_binary]
        if not candidates:
            return []

        findings: list[Finding] = []
        for adapter in self._selected_adapters():
            applicable = adapter.applicable(candidates)
            if not applicable or not adapter.is_available(self.repo, applicable):
                log.debug("skipping %s (unavailable or not applicable)", adapter.name)
                continue
            findings.extend(self._run_one(adapter, applicable, sandboxed=sandboxed))

        return self._restrict_to_changed_lines(findings, changeset)

    def _selected_adapters(self) -> list[Adapter]:
        if self.config.tools is None:
            return self.adapters
        wanted = {name.lower() for name in self.config.tools}
        return [a for a in self.adapters if a.name in wanted]

    def _run_one(
        self, adapter: Adapter, files: list[ChangedFile], *, sandboxed: list[str] | None
    ) -> list[Finding]:
        executable = adapter.find_binary(self.repo)
        if executable is None:
            return []

        # Existing files only: a tool given a path it cannot open usually aborts
        # the whole run rather than skipping that one file.
        paths = [f.path for f in files if (self.repo / f.path).is_file()]
        if not paths:
            return []

        run = adapter.build(executable, paths, self.repo)
        command = [*(sandboxed or []), *run.command]
        try:
            completed = subprocess.run(
                command,
                cwd=self.repo,
                capture_output=True,
                text=True,
                env=_safe_environment(),
                timeout=self.config.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired:
            log.warning(
                "%s timed out after %ds; skipping it", adapter.name, self.config.timeout_seconds
            )
            return []
        except OSError as exc:
            log.warning("could not run %s: %s", adapter.name, exc)
            return []

        try:
            findings = adapter.parse(completed.stdout, completed.stderr, completed.returncode)
        except Exception:  # a tool's output format is not our contract
            log.warning("could not parse %s output; skipping it", adapter.name, exc_info=True)
            return []

        log.debug("%s produced %d findings", adapter.name, len(findings))
        return [self._relativise(f) for f in findings]

    def _sandboxed_command(self) -> list[str] | None:
        """Prefix for a read-only, networkless CI execution, when required."""
        if self.config.execution is not StaticExecution.AUTO or not _in_ci():
            return None
        bwrap = shutil.which("bwrap")
        if bwrap is None:
            return None
        repo_parents = [
            item
            for parent in reversed(self.repo.resolve().parents)
            if parent != Path("/")
            for item in ("--dir", str(parent))
        ]
        return [
            bwrap,
            "--die-with-parent",
            "--unshare-net",
            "--ro-bind",
            "/usr",
            "/usr",
            "--ro-bind-try",
            "/bin",
            "/bin",
            "--ro-bind-try",
            "/lib",
            "/lib",
            "--ro-bind-try",
            "/lib64",
            "/lib64",
            "--ro-bind",
            "/etc",
            "/etc",
            *repo_parents,
            "--ro-bind",
            str(self.repo),
            str(self.repo),
            "--tmpfs",
            "/tmp",
            "--dev-bind",
            "/dev",
            "/dev",
            "--proc",
            "/proc",
            "--chdir",
            str(self.repo),
            "--",
        ]

    def _relativise(self, finding: Finding) -> Finding:
        """Tools report absolute paths; the rest of roborak speaks repo-relative."""
        # ValueError just means the path is already relative, or genuinely
        # outside the repo; either way there is nothing to rewrite.
        with contextlib.suppress(ValueError):
            finding.file = str(Path(finding.file).resolve().relative_to(self.repo.resolve()))
        return finding

    def _restrict_to_changed_lines(
        self, findings: list[Finding], changeset: ChangeSet
    ) -> list[Finding]:
        """Keep only what this change is responsible for.

        A linter run on a touched file reports the whole file, including problems
        that predate the change. Reporting those is how a review tool earns a
        reputation for noise, so they are dropped here rather than by the model.
        """
        kept: list[Finding] = []
        for finding in findings:
            file = changeset.file_by_path(finding.file)
            if file is None:
                continue
            added = file.added_lines
            if any(line in added for line in range(finding.start_line, finding.end_line + 1)):
                kept.append(finding)
        return kept


_SAFE_ENV = {
    "PATH",
    "PATHEXT",
    "SystemRoot",
    "LANG",
    "LANGUAGE",
    "LC_ALL",
    "LC_CTYPE",
    "TERM",
    "VIRTUAL_ENV",
}


def _safe_environment() -> dict[str, str]:
    """Static tools get runtime plumbing, never the caller's credentials."""
    env = {key: value for key, value in os.environ.items() if key in _SAFE_ENV}
    env.update(
        {
            "HOME": "/tmp",
            "TMPDIR": "/tmp",
            "XDG_CACHE_HOME": "/tmp/roborak-static-cache",
        }
    )
    return env


def _in_ci() -> bool:
    value = os.getenv("CI", "").strip().lower()
    return value not in {"", "0", "false", "no"}
