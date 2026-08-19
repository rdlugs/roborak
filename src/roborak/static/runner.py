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
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from roborak.core.config import StaticConfig
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
        if not self.config.enabled:
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
            findings.extend(self._run_one(adapter, applicable))

        return self._restrict_to_changed_lines(findings, changeset)

    def _selected_adapters(self) -> list[Adapter]:
        if self.config.tools is None:
            return self.adapters
        wanted = {name.lower() for name in self.config.tools}
        return [a for a in self.adapters if a.name in wanted]

    def _run_one(self, adapter: Adapter, files: list[ChangedFile]) -> list[Finding]:
        executable = adapter.find_binary(self.repo)
        if executable is None:
            return []

        # Existing files only: a tool given a path it cannot open usually aborts
        # the whole run rather than skipping that one file.
        paths = [f.path for f in files if (self.repo / f.path).is_file()]
        if not paths:
            return []

        run = adapter.build(executable, paths, self.repo)
        try:
            completed = subprocess.run(
                run.command,
                cwd=self.repo,
                capture_output=True,
                text=True,
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
