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

from roborak.core.config import StaticConfig, StaticExecution
from roborak.core.models import ChangedFile, ChangeSet, Finding
from roborak.sandbox import in_ci, safe_environment, sandbox_prefix
from roborak.static.adapters.actionlint import ActionlintAdapter
from roborak.static.adapters.base import Adapter
from roborak.static.adapters.checkov import CheckovAdapter
from roborak.static.adapters.eslint import EslintAdapter
from roborak.static.adapters.hadolint import HadolintAdapter
from roborak.static.adapters.mypy import MypyAdapter
from roborak.static.adapters.osv_scanner import OsvScannerAdapter
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
    # Supply-chain and infrastructure scanners. Availability-gated like every
    # other adapter; `osv-scanner` additionally needs naming in `static.tools`,
    # because it is the only one here that reaches the network.
    ActionlintAdapter(),
    HadolintAdapter(),
    CheckovAdapter(),
    OsvScannerAdapter(),
]


@dataclass(frozen=True)
class SkippedTool:
    """A tool that applied to this change and could not run."""

    name: str
    reason: str


@dataclass
class StaticRunner:
    repo: Path
    config: StaticConfig
    adapters: list[Adapter] = field(default_factory=lambda: list(ALL_ADAPTERS))

    skipped: list[SkippedTool] = field(default_factory=list)
    """Applicable tools that were not available, populated by ``run``.

    Kept beside the findings rather than returned with them so that no existing
    caller has to change shape. What reads it is the supply-chain report, where a
    missing container or workflow linter is part of the coverage story."""

    def run(self, changeset: ChangeSet) -> list[Finding]:
        """Every applicable adapter over the changed files, narrowed to the changed lines."""
        if not self.config.enabled or self.config.execution is StaticExecution.OFF:
            return []

        self.skipped.clear()
        sandboxed = self._sandboxed_command()
        if self.config.execution is StaticExecution.AUTO and in_ci() and sandboxed is None:
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
            if not applicable:
                continue
            if not adapter.is_available(self.repo, applicable):
                # Recorded rather than only logged: a reader who cannot tell "the
                # container linter found nothing" from "the container linter is
                # not installed" will read the first meaning into the second.
                reason = f"`{adapter.binary}` is not installed or configured in this checkout."
                self.skipped.append(SkippedTool(name=adapter.name, reason=reason))
                log.debug("skipping %s: %s", adapter.name, reason)
                continue
            findings.extend(self._run_one(adapter, applicable, sandboxed=sandboxed))

        return self._restrict_to_changed_lines(findings, changeset)

    def _selected_adapters(self) -> list[Adapter]:
        """The adapters this review may run.

        ``None`` means autodetect, and autodetect stays offline: a tool that
        reaches the network is included only when a project names it, so the
        default pass keeps the guarantee in this module's docstring.
        """
        if self.config.tools is None:
            return [a for a in self.adapters if not a.requires_network]
        wanted = {name.lower() for name in self.config.tools}
        return [a for a in self.adapters if a.name in wanted]

    def _run_one(
        self, adapter: Adapter, files: list[ChangedFile], *, sandboxed: list[str] | None
    ) -> list[Finding]:
        """One adapter over the files it applies to, behind the sandbox prefix when there is one."""
        executable = adapter.find_binary(self.repo)
        if executable is None:
            return []

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
                encoding="utf-8",
                errors="replace",
                env=safe_environment("/tmp" if sandboxed else None),
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
        except Exception:
            log.warning("could not parse %s output; skipping it", adapter.name, exc_info=True)
            return []

        log.debug("%s produced %d findings", adapter.name, len(findings))
        return [self._relativise(f) for f in findings]

    def _sandboxed_command(self) -> list[str] | None:
        """Prefix for a read-only, networkless CI execution, when required."""
        if self.config.execution is not StaticExecution.AUTO or not in_ci():
            return None
        return sandbox_prefix(self.repo)

    def _relativise(self, finding: Finding) -> Finding:
        """Tools report absolute paths; the rest of roborak speaks repo-relative."""
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
