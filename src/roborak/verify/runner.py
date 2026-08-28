"""Run the project's own tests against a checked-out change.

Static analysis can say a line looks wrong. It cannot say whether the behaviour
the change describes actually happens, and a review that flags missing coverage
while never running the suite leaves the reader to go and find out themselves.
This stage closes that gap with execution evidence: the narrowest matching set of
configured commands that covers the changed files, each run once, recorded with
its exit status.

Design rules, in the order they bind:

- **Proportional.** A change to one module runs that module's checks, not the
  whole suite. The broad check is reserved for a change that crosses a shared
  boundary, where a targeted run could not speak for what it broke.
- **Never inferred.** Commands come from configuration a change cannot write --
  see ``core.config.load_verification`` -- and never from guessing at a
  package manifest. Executing what a diff asked for is not verification.
- **Never fatal.** A missing executable, a sandbox that refuses, a suite that
  hangs: each is recorded and reported, and none of them stops the review.
- **Nothing is installed and nothing is fetched.** The suite runs against the
  checkout as it stands, with the same credential-scrubbed environment and the
  same CI sandbox the static pass gets.
"""

from __future__ import annotations

import fnmatch
import logging
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from roborak.core.config import Execution, VerificationConfig
from roborak.core.models import (
    ChangeSet,
    VerificationReport,
    VerificationRun,
    VerificationScope,
    VerificationStatus,
)
from roborak.sandbox import in_ci, safe_environment, sandbox_prefix

log = logging.getLogger(__name__)

MAX_OUTPUT_CHARS = 8000
"""Hard ceiling on the output kept per command, on top of ``max_output_lines``.

A line limit bounds a chatty runner; it does not bound a single line. One failing
assertion that prints a megabyte-long repr is still one line, and it would travel
whole into the prompt and the published comment. Counted in characters because
that is the dimension the line limit does not cover."""

CHECKED_OUT_ORIGINS = {"local", "paths"}
"""Origins whose files are on disk. A forge diff is a patch we fetched, not a
tree we can run, and running the local checkout against it would verify whatever
happens to be in the working directory instead of the change under review."""


def select(config: VerificationConfig, changeset: ChangeSet) -> list[VerificationRun]:
    """The narrowest command set that covers this change, as unstarted runs.

    Three rules, applied in order:

    1. a change touching a broadening path -- a shared contract, a schema, the
       build configuration -- selects the broad check *instead of* the targeted
       ones, because a full suite already contains every subset of itself and
       running both is the same work twice;
    2. otherwise every configured command whose globs match a changed path is
       selected, deduplicated by argv so two path rules pointing at one command
       run it once;
    3. a change that matched nothing falls back to the broad check, which is the
       only honest answer when the configuration does not describe these files.
    """
    paths = [
        file.path
        for file in changeset.files
        if file.change_type != "deleted" and not file.is_binary
    ]
    if not paths:
        return []

    broad = _broad_run(config)
    if broad is not None and _any_match(paths, config.broaden_paths):
        return [broad]

    runs: list[VerificationRun] = []
    seen: set[tuple[str, ...]] = set()
    for entry in config.commands:
        if not _any_match(paths, entry.paths):
            continue
        key = tuple(entry.command)
        if key in seen:
            continue
        seen.add(key)
        runs.append(
            VerificationRun(
                name=entry.name or " ".join(entry.command),
                command=list(entry.command),
                scope=VerificationScope.TARGETED,
            )
        )

    if not runs and broad is not None:
        runs.append(broad)
    return runs[: config.max_commands]


def _broad_run(config: VerificationConfig) -> VerificationRun | None:
    if not config.fallback:
        return None
    return VerificationRun(
        name=" ".join(config.fallback),
        command=list(config.fallback),
        scope=VerificationScope.BROAD,
    )


def _any_match(paths: list[str], patterns: list[str]) -> bool:
    return any(_matches(path, pattern) for path in paths for pattern in patterns)


def _matches(path: str, pattern: str) -> bool:
    """Glob matching as the rest of roborak does it, ``**/`` optional at the front."""
    if fnmatch.fnmatch(path, pattern):
        return True
    return pattern.startswith("**/") and fnmatch.fnmatch(path, pattern[3:])


@dataclass
class VerificationRunner:
    repo: Path
    config: VerificationConfig
    source: str = ""
    """Where the commands came from, carried onto the report for the reader."""

    notes: list[str] = field(default_factory=list)
    """Notes from resolving the configuration, e.g. a working-tree section that
    was deliberately not read."""

    def run(self, changeset: ChangeSet) -> VerificationReport | None:
        """Verify the change, or return ``None`` when nobody asked for verification.

        ``None`` and an empty report say different things and both are reachable:
        no configuration at all means the review is static-only and says nothing
        about tests, while a report full of skipped runs means commands were
        selected and something stopped them.
        """
        if not self._configured():
            return None

        report = VerificationReport(source=self.source, notes=list(self.notes))
        runs = select(self.config, changeset)
        if not runs:
            report.notes.append(
                "No configured verification command matches the files this change touches."
            )
            return report

        report.runs = runs
        if reason := self._refusal(changeset):
            for run in runs:
                run.note = reason
            report.notes.append(reason)
            return report

        sandboxed = sandbox_prefix(self.repo) if self._needs_sandbox() else None
        if sandboxed is not None:
            report.notes.append(
                "Commands ran in a read-only, networkless sandbox because the CI checkout is "
                "untrusted. A suite that needs to write is reported as errored, not failed."
            )
        for run in runs:
            self._execute(run, sandboxed=sandboxed)
        return report

    def _configured(self) -> bool:
        return (
            self.config.enabled
            and self.config.execution is not Execution.OFF
            and bool(self.config.commands or self.config.fallback)
        )

    def _refusal(self, changeset: ChangeSet) -> str:
        """Why nothing may run, or ``""`` when it may.

        Both answers here are about trust rather than about the commands. A diff
        we never checked out cannot be verified by running the tree we did check
        out, and an untrusted CI checkout without a sandbox is exactly the case
        the static pass refuses too.
        """
        if changeset.origin not in CHECKED_OUT_ORIGINS:
            return (
                f"Skipped: {changeset.origin} changes are not checked out, and running the local "
                "tree would verify something other than this change."
            )
        if self._needs_sandbox() and sandbox_prefix(self.repo) is None:
            return (
                "Skipped: the CI checkout is untrusted and bubblewrap is unavailable. Use "
                "--trust-verify only when the checkout and its test commands are trusted."
            )
        return ""

    def _needs_sandbox(self) -> bool:
        return self.config.execution is Execution.AUTO and in_ci()

    def _execute(self, run: VerificationRun, *, sandboxed: list[str] | None) -> None:
        command = [*(sandboxed or []), *run.command]
        started = time.monotonic()
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
        except subprocess.TimeoutExpired as exc:
            run.status = VerificationStatus.TIMED_OUT
            run.duration_ms = _elapsed_ms(started)
            run.note = f"No result after {self.config.timeout_seconds}s; the command was killed."
            self._record_output(run, _decode(exc.stdout), _decode(exc.stderr))
            log.warning("%s timed out after %ds", run.name, self.config.timeout_seconds)
            return
        except OSError as exc:
            # A missing executable is the common one, and it is a statement about
            # this machine rather than about the change -- so it is never `failed`.
            run.status = VerificationStatus.ERRORED
            run.duration_ms = _elapsed_ms(started)
            run.note = f"Could not run the command: {exc}"
            log.warning("could not run %s: %s", run.name, exc)
            return

        run.duration_ms = _elapsed_ms(started)
        run.exit_code = completed.returncode
        run.status = (
            VerificationStatus.PASSED if completed.returncode == 0 else VerificationStatus.FAILED
        )
        self._record_output(run, completed.stdout, completed.stderr)

    def _record_output(self, run: VerificationRun, stdout: str, stderr: str) -> None:
        """Keep the tail, which is where a test runner puts its verdict.

        Bounded twice, because a line count is not a size: ``max_output_lines``
        bounds a runner that says too much, and ``MAX_OUTPUT_CHARS`` bounds the
        one line that says too much at once. Both keep the tail -- this is a
        citation on a review, not a build log, and the head of a failing suite is
        a progress bar. The subprocess was handed a credential-scrubbed
        environment, so what lands here has no token in it to begin with -- see
        ``sandbox.safe_environment``.
        """
        combined = "\n".join(part.strip() for part in (stdout, stderr) if part.strip())
        lines = combined.splitlines()
        if len(lines) > self.config.max_output_lines:
            run.truncated = True
            lines = lines[-self.config.max_output_lines :]
        output = "\n".join(lines)
        if len(output) > MAX_OUTPUT_CHARS:
            run.truncated = True
            output = output[-MAX_OUTPUT_CHARS:]
        run.output = output


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _decode(stream: str | bytes | None) -> str:
    """``TimeoutExpired`` hands back whatever was captured, sometimes as bytes."""
    if stream is None:
        return ""
    if isinstance(stream, bytes):
        return stream.decode("utf-8", errors="replace")
    return stream


def for_prompt(report: VerificationReport | None) -> dict[str, Any] | None:
    """The report as evidence for the model, or ``None`` when there is none.

    ``None`` means the stage was never configured, and nothing else does: a report
    with no runs still says that verification was asked for and that nothing
    matched, which is a different fact and one the model should not have to infer
    from silence. Its notes carry the reason, so they travel with it.

    Deliberately asymmetric. A failing command is the whole point of the stage and
    travels with its output; a passing one is worth a line, because "it passed" is
    the entire content of a green run and pasting the summary of a thousand tests
    would crowd out the diff it is meant to be evidence about.
    """
    if report is None:
        return None
    return {
        "status": report.status.value,
        "executed": report.executed,
        "notes": list(report.notes),
        "runs": [
            {
                "name": run.name,
                "command": run.display_command,
                "status": run.status.value,
                "exit_code": run.exit_code,
                "scope": run.scope.value,
                "note": run.note,
                "output": run.output if run.status is not VerificationStatus.PASSED else "",
            }
            for run in report.runs
        ],
    }
