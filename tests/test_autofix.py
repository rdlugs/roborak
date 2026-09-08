"""Autofix uses real diffs and never moves replacement anchors."""

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import NoReturn

import pytest
import yaml
from typer.testing import CliRunner

from roborak.analysis import autofix, validator
from roborak.cli.commands.setup_cmd import Aborted
from roborak.cli.main import app
from roborak.core.config import Config
from roborak.core.models import Finding, ReviewResult, ReviewStatus
from roborak.llm.parser import ParseError, parse_findings
from roborak.sources.local_git import LocalGitSource, Scope


def git(repo: Path, *args: str) -> bytes:
    return subprocess.check_output(["git", *args], cwd=repo)


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr("roborak.core.config.USER_CONFIG_PATH", tmp_path / "absent")
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.email", "test@example.com")
    git(tmp_path, "config", "user.name", "Test")
    (tmp_path / "app.py").write_bytes(b"def f():\n    return 1\n")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "Initial")
    (tmp_path / "app.py").write_bytes(b"def f():\n    return 2\n")
    return tmp_path


def finding(**kwargs: object) -> Finding:
    return Finding.model_validate(
        dict(
            file="app.py",
            start_line=2,
            end_line=2,
            title="Improve return",
            body="Use the correct value.",
            suggestion="    return 3\n",
            severity="minor",
            category="maintainability",
        )
        | kwargs
    )


def plan_for(repo: Path, findings: list[Finding] | None = None) -> autofix.FixPlan:
    changes = LocalGitSource(repo, scope=Scope.UNCOMMITTED).load()
    plan = autofix.capture(repo, changes)
    findings = findings if findings is not None else [finding()]
    result = ReviewResult(changeset=changes, findings=[f.model_copy(deep=True) for f in findings])
    autofix.prepare(plan, findings, result)
    return plan


def test_apply_keeps_index(repo: Path) -> None:
    git(repo, "add", "app.py")
    before = git(repo, "show", ":app.py")
    plan = plan_for(repo)
    autofix.apply(plan)
    assert (repo / "app.py").read_bytes() == b"def f():\n    return 3\n"
    assert git(repo, "show", ":app.py") == before
    assert plan.report.items[0].outcome == "applied"


@pytest.mark.parametrize("content", [b"def f():\r\n    return 2\r\n", b"def f():\n    return 2"])
def test_newline_preserved(repo: Path, content: bytes) -> None:
    (repo / "app.py").write_bytes(content)
    plan = plan_for(repo)
    autofix.apply(plan)
    assert (repo / "app.py").read_bytes() == content.replace(b"return 2", b"return 3")


def test_disjoint_length_changing_edits(repo: Path) -> None:
    (repo / "app.py").write_text("a = 2\nb = 2\nc = 2\n")
    plan = plan_for(
        repo,
        [
            finding(start_line=1, end_line=1, suggestion="a = 3\nx = 4\n"),
            finding(start_line=3, end_line=3, suggestion="c = 3\n", title="Other"),
        ],
    )
    autofix.apply(plan)
    assert (repo / "app.py").read_text() == "a = 3\nx = 4\nb = 2\nc = 3\n"


@pytest.mark.parametrize(
    "kwargs,reason",
    [
        ({"suggestion": None}, "committable"),
        ({"end_line": 20}, "outside"),
        ({"start_line": 1, "end_line": 1}, "intersect"),
        ({"suggestion": "    return 2\n"}, "no change"),
    ],
)
def test_skips(repo: Path, kwargs: dict[str, object], reason: str) -> None:
    plan = plan_for(repo, [finding(**kwargs)])
    assert plan.report.items[0].outcome == "skipped"
    assert reason in plan.report.items[0].reason
    assert not plan.replacements


def test_overlap_skips_both(repo: Path) -> None:
    plan = plan_for(repo, [finding(), finding(title="Other", suggestion="    return 4")])
    assert all(i.outcome == "skipped" and "Overlapping" in i.reason for i in plan.report.items)


def test_moved_anchor_rejected(repo: Path) -> None:
    changes = LocalGitSource(repo, scope=Scope.UNCOMMITTED).load()
    candidate = finding(start_line=1, end_line=1)
    accepted = validator.validate([candidate.model_copy(deep=True)], changes, Config())
    assert accepted[0].start_line == 2
    plan = autofix.capture(repo, changes)
    autofix.prepare(plan, [candidate], ReviewResult(changeset=changes, findings=accepted))
    assert "anchor moved" in plan.report.items[0].reason


def test_concurrent_edit_skipped(repo: Path) -> None:
    plan = plan_for(repo)
    (repo / "app.py").write_text("# moved\ndef f():\n    return 2\n")
    autofix.apply(plan)
    assert plan.report.items[0].outcome == "skipped"
    assert (repo / "app.py").read_text().startswith("# moved")


@pytest.mark.parametrize("atomic_save", [False, True])
def test_save_after_final_target_validation_survives(
    repo: Path, monkeypatch: pytest.MonkeyPatch, atomic_save: bool
) -> None:
    plan = plan_for(repo)
    target = repo / "app.py"
    newer = b"# concurrent save\ndef f():\n    return 4\n"
    replace = autofix.os.replace

    def save_then_capture(source: str | Path, destination: str | Path) -> None:
        if Path(source) == target:
            if atomic_save:
                saved = repo / "editor-save"
                saved.write_bytes(newer)
                replace(saved, target)
            else:
                target.write_bytes(newer)
        replace(source, destination)

    monkeypatch.setattr(autofix.os, "replace", save_then_capture)
    autofix.apply(plan)
    assert target.read_bytes() == newer
    assert plan.report.items[0].outcome == "failed"
    assert "Target changed" in plan.report.items[0].reason
    assert not list(repo.glob(".roborak-fix-*"))


def test_save_after_captured_validation_survives(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = plan_for(repo)
    target = repo / "app.py"
    newer = b"# concurrent save\ndef f():\n    return 4\n"
    matches = autofix._matches_snapshot

    def save_after_validation(path: Path, snapshot: autofix.Snapshot) -> bool:
        matched = matches(path, snapshot)
        target.write_bytes(newer)
        return matched

    monkeypatch.setattr(autofix, "_matches_snapshot", save_after_validation)
    autofix.apply(plan)
    assert target.read_bytes() == newer
    assert plan.report.items[0].outcome == "failed"
    recovery = next(repo.glob(".roborak-fix-*")) / "original"
    assert recovery.read_bytes() == plan.snapshots["app.py"].content
    assert str(recovery) in plan.report.items[0].reason


def test_publication_failure_restores_original(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plan = plan_for(repo)
    target = repo / "app.py"
    link = autofix.os.link

    def fail_publication(
        source: str | Path, destination: str | Path, *, follow_symlinks: bool = True
    ) -> None:
        if Path(source).name == "replacement" and Path(destination) == target:
            raise OSError("publication failed")
        link(source, destination, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(autofix.os, "link", fail_publication)
    autofix.apply(plan)
    assert target.read_bytes() == plan.snapshots["app.py"].content
    assert plan.report.items[0].outcome == "failed"
    assert "publication failed" in plan.report.items[0].reason
    assert not list(repo.glob(".roborak-fix-*"))


def test_unsupported_links_leave_target_in_place(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = plan_for(repo)

    def unsupported(*args: object, **kwargs: object) -> NoReturn:
        raise OSError("hard links unsupported")

    monkeypatch.setattr(autofix.os, "link", unsupported)
    autofix.apply(plan)
    assert (repo / "app.py").read_bytes() == plan.snapshots["app.py"].content
    assert plan.report.items[0].outcome == "failed"
    assert not list(repo.glob(".roborak-fix-*"))


@pytest.mark.skipif(sys.platform == "win32", reason="Windows does not rename open files.")
def test_open_descriptor_save_preserved_for_recovery(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = plan_for(repo)
    target = repo / "app.py"
    newer = b"# saved via open descriptor\n"
    link = autofix.os.link
    with target.open("r+b") as editor:

        def save_before_publication(
            source: str | Path, destination: str | Path, *, follow_symlinks: bool = True
        ) -> None:
            if Path(source).name == "replacement" and Path(destination) == target:
                editor.write(newer)
                editor.truncate()
                editor.flush()
            link(source, destination, follow_symlinks=follow_symlinks)

        monkeypatch.setattr(autofix.os, "link", save_before_publication)
        autofix.apply(plan)
    assert plan.report.items[0].outcome == "failed"
    recovery = next(repo.glob(".roborak-fix-*")) / "original"
    assert recovery.read_bytes() == newer
    assert str(recovery) in plan.report.items[0].reason


def test_failed_generation(repo: Path) -> None:
    changes = LocalGitSource(repo, scope=Scope.UNCOMMITTED).load()
    plan = autofix.capture(repo, changes)
    autofix.prepare(plan, [finding()], ReviewResult(changeset=changes, status=ReviewStatus.PARTIAL))
    assert plan.report.errors and not plan.replacements


@pytest.mark.parametrize("name", ["../outside", "/tmp/outside", ".git/config", "missing"])
def test_unsafe_targets(repo: Path, name: str) -> None:
    with pytest.raises(ValueError):
        autofix.read_target(repo, name)


def test_symlink_rejected(repo: Path) -> None:
    (repo / "app.py").unlink()
    (repo / "app.py").symlink_to(repo / ".git/config")
    with pytest.raises(ValueError, match="Symlink"):
        autofix.read_target(repo, "app.py")


def test_forge_requires_clean_exact_head(repo: Path) -> None:
    changes = LocalGitSource(repo, scope=Scope.UNCOMMITTED).load()
    changes.origin = "github"
    with pytest.raises(ValueError, match="clean checkout"):
        autofix.capture(repo, changes)
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "Change")
    with pytest.raises(ValueError, match="HEAD differs"):
        autofix.capture(repo, changes)
    changes.head_sha = git(repo, "rev-parse", "HEAD").decode().strip()
    plan = autofix.capture(repo, changes)
    autofix.prepare(plan, [finding()], ReviewResult(changeset=changes, findings=[finding()]))
    autofix.apply(plan)
    assert plan.report.items[0].outcome == "applied"


def test_write_failure_reported(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plan = plan_for(repo)

    def fail(*args: object) -> NoReturn:
        raise OSError("write denied")

    monkeypatch.setattr(autofix.os, "replace", fail)
    autofix.apply(plan)
    assert plan.report.items[0].outcome == "failed"
    assert (repo / "app.py").read_text().endswith("return 2\n")
    assert not list(repo.glob("tmp*"))


@pytest.mark.parametrize("suggestion", ["    return 3\n", "+value\n", "  x  \n"])
def test_parser_preserves_verbatim(suggestion: str) -> None:
    raw = finding(suggestion=suggestion).model_dump(mode="json")
    parsed = parse_findings(yaml.safe_dump({"findings": [raw]}), autofix=True)
    assert parsed[0].suggestion == suggestion


@pytest.mark.parametrize(
    "update",
    [
        {"end_line": 0},
        {"start_line": "2"},
        {"suggestion": "```python\nx\n```"},
        {"suggestion": "diff --git a/x b/x"},
    ],
)
def test_parser_rejects_unsafe_replacement(update: dict[str, object]) -> None:
    raw = finding().model_dump(mode="json") | update
    assert parse_findings(yaml.safe_dump({"findings": [raw]}), autofix=True)[0].suggestion is None


def test_parser_refuses_truncated_response() -> None:
    with pytest.raises(ParseError):
        parse_findings("findings:\n  - file: [unfinished", autofix=True)


def test_noninteractive_requires_explicit_mode() -> None:
    result = CliRunner().invoke(app, ["fix"])
    assert result.exit_code == 2 and "--yes or --dry-run" in result.output


@pytest.mark.parametrize("mode", ["--dry-run", "--yes"])
def test_cli_json(repo: Path, monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    stub_model(monkeypatch)
    result = CliRunner().invoke(app, ["fix", "-C", str(repo), "--uncommitted", mode, "--json"])
    assert result.exit_code == 0, result.output
    report = json.loads(result.stdout)
    assert report["items"][0]["outcome"] == ("eligible" if mode == "--dry-run" else "applied")
    assert (
        (repo / "app.py")
        .read_text()
        .endswith("return 2\n" if mode == "--dry-run" else "return 3\n")
    )


def test_forge_patch_only(repo: Path) -> None:
    changes = LocalGitSource(repo, scope=Scope.UNCOMMITTED).load()
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "Change")
    changes.head_sha = git(repo, "rev-parse", "HEAD").decode().strip()
    changes.origin = "gitlab"
    changes.files[0].new_content = None
    plan = autofix.capture(repo, changes)
    assert not plan.rejected
    assert changes.files[0].new_content == (repo / "app.py").read_text()


def test_patch_snapshot_disagreement(repo: Path) -> None:
    changes = LocalGitSource(repo, scope=Scope.UNCOMMITTED).load()
    changes.files[0].hunks[0].content = (
        changes.files[0].hunks[0].content.replace("return 2", "return 9")
    )
    plan = autofix.capture(repo, changes)
    assert "Patch context" in plan.rejected["app.py"]


def test_conflict_refused(repo: Path) -> None:
    blob = git(repo, "rev-parse", "HEAD:app.py").decode().strip()
    subprocess.run(
        ["git", "update-index", "--index-info"],
        cwd=repo,
        check=True,
        input=f"100644 {blob} 1\tapp.py\n100644 {blob} 2\tapp.py\n".encode(),
    )
    with pytest.raises(ValueError, match="merge conflicts"):
        plan_for(repo)


def test_head_changes_after_preview(repo: Path) -> None:
    plan = plan_for(repo)
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "Moved")
    autofix.apply(plan)
    assert plan.report.errors
    assert plan.report.items[0].outcome == "skipped"


def test_plain_directory_refused(repo: Path) -> None:
    from roborak.core.models import ChangeSet

    with pytest.raises(ValueError, match="Git checkout"):
        autofix.capture(repo, ChangeSet(origin="paths"))


@pytest.mark.parametrize("content", [b"def f():\r\n    return 2\n", b"def f():\r    return 2\r"])
def test_mixed_endings_skip(repo: Path, content: bytes) -> None:
    (repo / "app.py").write_bytes(content)
    plan = plan_for(repo)
    assert not plan.replacements


@pytest.mark.parametrize("content", [b"\xff", b"a\0b"])
def test_nontext_snapshot_rejected(repo: Path, content: bytes) -> None:
    (repo / "app.py").write_bytes(content)
    with pytest.raises(ValueError):
        autofix.read_target(repo, "app.py")


def test_no_final_newline_patch(repo: Path) -> None:
    (repo / "app.py").write_bytes(b"def f():\n    return 2")
    plan = plan_for(repo)
    assert "\\ No newline at end of file\n" in plan.report.patches["app.py"]


@pytest.mark.parametrize("accept", [True, False])
def test_interactive_confirmation(
    repo: Path, monkeypatch: pytest.MonkeyPatch, accept: bool
) -> None:
    monkeypatch.setattr("roborak.cli.shared.is_interactive", lambda: True)
    stub_model(monkeypatch)
    result = CliRunner().invoke(
        app, ["fix", "-C", str(repo), "--uncommitted"], input="y\n" if accept else "n\n"
    )
    assert result.exit_code == 0, result.output
    assert "Apply these fixes?" in result.stderr
    assert (repo / "app.py").read_text().endswith("return 3\n" if accept else "return 2\n")


@pytest.mark.parametrize("interruption", [EOFError, KeyboardInterrupt])
def test_interactive_confirmation_aborted(
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    interruption: type[EOFError] | type[KeyboardInterrupt],
) -> None:
    monkeypatch.setattr("roborak.cli.shared.is_interactive", lambda: True)
    stub_model(monkeypatch)
    before = (repo / "app.py").read_bytes()
    index_before = git(repo, "show", ":app.py")

    def interrupt(*args: object, **kwargs: object) -> NoReturn:
        raise interruption

    monkeypatch.setattr("rich.console.Console.input", interrupt)
    result = CliRunner().invoke(app, ["fix", "-C", str(repo), "--uncommitted"])
    assert isinstance(result.exception, Aborted)
    assert result.exit_code != 0
    assert not result.stdout
    assert (repo / "app.py").read_bytes() == before
    assert git(repo, "show", ":app.py") == index_before


@pytest.mark.parametrize("finish_reason", ["length", [], {}, 42])
def test_incomplete_or_invalid_generation_never_applies(
    repo: Path, monkeypatch: pytest.MonkeyPatch, finish_reason: object
) -> None:
    stub_model(monkeypatch, finish_reason=finish_reason)
    result = CliRunner().invoke(app, ["fix", "-C", str(repo), "--uncommitted", "--yes", "--json"])
    assert result.exit_code == 2, result.output
    assert json.loads(result.stdout)["errors"]
    assert (repo / "app.py").read_text().endswith("return 2\n")


def stub_model(monkeypatch: pytest.MonkeyPatch, finish_reason: object = "stop") -> None:
    monkeypatch.setattr("roborak.cli.shared.missing_credentials", lambda *args: None)
    monkeypatch.setattr(
        "litellm.completion",
        lambda **kwargs: SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=yaml.safe_dump({"findings": [finding().model_dump(mode="json")]})
                    ),
                    finish_reason=finish_reason,
                )
            ]
        ),
    )


def test_failure_keeps_other_file_success(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (repo / "other.py").write_text("x = 1\n")
    git(repo, "add", "other.py")
    plan = plan_for(
        repo, [finding(), finding(file="other.py", start_line=1, end_line=1, suggestion="x = 2\n")]
    )
    replace = autofix.os.replace

    def selective_failure(source: str | Path, target: Path) -> None:
        if Path(source).name == "app.py":
            raise OSError("write denied")
        replace(source, target)

    monkeypatch.setattr(autofix.os, "replace", selective_failure)
    autofix.apply(plan)
    assert [i.outcome for i in plan.report.items] == ["failed", "applied"]
    assert (repo / "other.py").read_text() == "x = 2\n"
