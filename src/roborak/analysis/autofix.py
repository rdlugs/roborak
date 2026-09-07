"""Snapshot-bound replacements for validated suggestions."""

from __future__ import annotations

import difflib
import os
import stat
import subprocess
import tempfile
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from roborak.core.models import ChangeSet, Finding, ReviewResult, ReviewStatus


class FixItem(BaseModel):
    finding: Finding
    outcome: Literal["eligible", "applied", "skipped", "failed"] = "skipped"
    reason: str = ""


class FixReport(BaseModel):
    dry_run: bool = False
    items: list[FixItem] = Field(default_factory=list)
    patches: dict[str, str] = Field(default_factory=dict)
    errors: list[str] = Field(default_factory=list)


class Snapshot(BaseModel):
    content: bytes
    device: int
    inode: int
    mode: int


class FixPlan(BaseModel):
    repo: Path
    head: str
    forge: bool
    snapshots: dict[str, Snapshot] = Field(default_factory=dict)
    rejected: dict[str, str] = Field(default_factory=dict)
    replacements: dict[str, bytes] = Field(default_factory=dict)
    report: FixReport = Field(default_factory=FixReport)


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise ValueError(f"Cannot inspect Git state: {result.stderr.strip()}")
    return result.stdout


def check_tree(repo: Path, head: str, *, forge: bool) -> None:
    if git(repo, "rev-parse", "HEAD").strip() != head:
        raise ValueError("Checkout HEAD differs from the reviewed head.")
    if git(repo, "ls-files", "--unmerged", "-z"):
        raise ValueError("Resolve merge conflicts before running fix.")
    if forge and git(repo, "status", "--porcelain").strip():
        raise ValueError("PR/MR fixes require a clean checkout at the reviewed head.")


def read_target(repo: Path, name: str) -> Snapshot:
    relative = Path(name)
    if (
        relative.is_absolute()
        or not relative.parts
        or any(part == ".." or part.lower() == ".git" for part in relative.parts)
    ):
        raise ValueError("Unsafe target path.")
    path = repo
    for part in relative.parts:
        path = path / part
        if path.is_symlink():
            raise ValueError("Symlink targets are unsupported.")
    if not path.resolve().is_relative_to(repo):
        raise ValueError("Target escapes the repository.")
    entries = git(repo, "ls-files", "--stage", "-z", "--", name).split("\0")
    if not any(
        entry.partition("\t")[2] == name and entry.startswith(("100644 ", "100755 "))
        for entry in entries
        if entry
    ):
        raise ValueError("Target must be a tracked regular file.")
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError("Target must be a regular file without hard links.")
    content = path.read_bytes()
    content.decode("utf-8")
    if b"\0" in content:
        raise ValueError("Binary targets are unsupported.")
    return Snapshot(content=content, device=info.st_dev, inode=info.st_ino, mode=info.st_mode)


def capture(repo: Path, changeset: ChangeSet) -> FixPlan:
    if changeset.origin == "paths":
        raise ValueError("fix requires a Git checkout.")
    root = Path(git(repo, "rev-parse", "--show-toplevel").strip()).resolve()
    if root != repo.resolve():
        raise ValueError("Run fix from the repository root, or pass --dir with its root.")
    plan = FixPlan(repo=root, head=changeset.head_sha, forge=changeset.origin != "local")
    check_tree(root, plan.head, forge=plan.forge)
    for file in changeset.files:
        try:
            snapshot = read_target(root, file.path)
            text = snapshot.content.decode("utf-8").replace("\r\n", "\n")
            if file.is_binary or file.change_type == "deleted" or file.patch_unavailable:
                raise ValueError("Target has no usable text patch.")
            if plan.forge and file.new_content is None:
                # The verified head supplies full content when the forge only sent a patch.
                file.new_content = text
            for hunk in file.hunks:
                expected = [
                    line[1:] for line in hunk.content.splitlines() if line.startswith((" ", "+"))
                ]
                actual = text.splitlines()[hunk.new_start - 1 : hunk.new_start - 1 + len(expected)]
                if expected != actual:
                    raise ValueError("Patch context differs from the snapshot.")
            if file.new_content is None or text != file.new_content.replace("\r\n", "\n"):
                raise ValueError(
                    "Snapshot differs from reviewed content or content is unavailable."
                )
            plan.snapshots[file.path] = snapshot
        except (OSError, ValueError) as exc:
            plan.rejected[file.path] = str(exc)
    return plan


def prepare(plan: FixPlan, candidates: list[Finding], result: ReviewResult) -> None:
    report = plan.report
    if result.errors or result.status is not ReviewStatus.COMPLETE:
        report.errors.extend(result.errors or ["Suggestion generation was incomplete."])
    for finding in candidates:
        item = FixItem(finding=finding)
        report.items.append(item)
        snapshot = plan.snapshots.get(finding.file)
        accepted = any(
            f.fingerprint == finding.fingerprint
            and f.file == finding.file
            and (f.start_line, f.end_line, f.suggestion)
            == (finding.start_line, finding.end_line, finding.suggestion)
            for f in result.findings
        )
        file = result.changeset.file_by_path(finding.file) if result.changeset else None
        if report.errors:
            item.reason = "Suggestion generation was incomplete."
        elif not finding.suggestion:
            item.reason = "No committable replacement or malformed range."
        elif not accepted:
            item.reason = "Rejected by validation or anchor moved."
        elif snapshot is None:
            item.reason = plan.rejected.get(finding.file, "No reviewed snapshot.")
        elif not (
            1 <= finding.start_line <= finding.end_line <= len(snapshot.content.splitlines())
        ):
            item.reason = "Replacement range is outside the file."
        elif not file or not file.added_lines.intersection(
            range(finding.start_line, finding.end_line + 1)
        ):
            item.reason = "Replacement does not intersect changed lines."
        elif any(
            other is not finding
            and other.suggestion
            and other.file == finding.file
            and finding.start_line <= other.end_line
            and other.start_line <= finding.end_line
            for other in candidates
        ):
            item.reason = "Overlapping suggestions are ambiguous."
        else:
            item.outcome = "eligible"
    for name, snapshot in plan.snapshots.items():
        items = [i for i in report.items if i.finding.file == name and i.outcome == "eligible"]
        lines = snapshot.content.splitlines(keepends=True)
        newline = b"\r\n" if b"\r\n" in snapshot.content else b"\n"
        if b"\r" in snapshot.content.replace(b"\r\n", b"") or (
            newline == b"\r\n" and b"\n" in snapshot.content.replace(b"\r\n", b"")
        ):
            for item in items:
                item.outcome, item.reason = "skipped", "Mixed or unsupported line endings."
            continue
        for item in sorted(items, key=lambda i: i.finding.start_line, reverse=True):
            f = item.finding
            replacement = (f.suggestion or "").replace("\r\n", "\n").encode("utf-8")
            replacement = replacement.removesuffix(b"\n").replace(b"\n", newline)
            if f.end_line < len(snapshot.content.splitlines()) or snapshot.content.endswith(b"\n"):
                replacement += newline
            original = b"".join(lines[f.start_line - 1 : f.end_line])
            if original == replacement:
                item.outcome, item.reason = "skipped", "Replacement makes no change."
                continue
            lines[f.start_line - 1 : f.end_line] = [replacement]
        updated = b"".join(lines)
        if updated != snapshot.content:
            plan.replacements[name] = updated
            report.patches[name] = "".join(
                line if line.endswith("\n") else line + "\n\\ No newline at end of file\n"
                for line in difflib.unified_diff(
                    snapshot.content.decode().splitlines(keepends=True),
                    updated.decode().splitlines(keepends=True),
                    fromfile=f"a/{name}",
                    tofile=f"b/{name}",
                )
            )


def apply(plan: FixPlan) -> None:
    try:
        check_tree(plan.repo, plan.head, forge=plan.forge)
    except (OSError, ValueError) as exc:
        plan.report.errors.append(str(exc))
        cancel(plan, str(exc))
        return
    for name, replacement in plan.replacements.items():
        items = [i for i in plan.report.items if i.finding.file == name and i.outcome == "eligible"]
        temporary: str | None = None
        try:
            check_tree(plan.repo, plan.head, forge=False)
            if read_target(plan.repo, name) != plan.snapshots[name]:
                for item in items:
                    item.outcome, item.reason = "skipped", "Target changed since the snapshot."
                continue
            with tempfile.NamedTemporaryFile(dir=(plan.repo / name).parent, delete=False) as out:
                temporary = out.name
                out.write(replacement)
            os.chmod(temporary, stat.S_IMODE(plan.snapshots[name].mode))
            if read_target(plan.repo, name) != plan.snapshots[name]:
                for item in items:
                    item.outcome, item.reason = "skipped", "Target changed before writing."
                continue
            os.replace(temporary, plan.repo / name)
            for item in items:
                item.outcome, item.reason = "applied", "Replacement applied."
        except (OSError, ValueError) as exc:
            for item in items:
                item.outcome, item.reason = "failed", str(exc)
        finally:
            if temporary is not None:
                Path(temporary).unlink(missing_ok=True)


def cancel(plan: FixPlan, reason: str = "Cancelled by user.") -> None:
    for item in plan.report.items:
        if item.outcome == "eligible":
            item.outcome, item.reason = "skipped", reason
