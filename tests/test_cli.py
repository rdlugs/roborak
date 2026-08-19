"""CLI surface tests, driven through Typer's runner."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from roborak.cli.commands.review import EXIT_ERROR, EXIT_FINDINGS, EXIT_OK
from roborak.cli.main import app

runner = CliRunner()


@pytest.fixture
def repo(tmp_path: Path, monkeypatch) -> Path:
    # Keep a real user config from leaking into assertions.
    monkeypatch.setattr("roborak.core.config.USER_CONFIG_PATH", tmp_path / "absent.yaml")
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
    for key, value in (("user.email", "t@example.com"), ("user.name", "T")):
        subprocess.run(["git", "config", key, value], cwd=tmp_path, check=True)
    (tmp_path / "app.py").write_text("def f():\n    return 1\n")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True)
    return tmp_path


def test_help_lists_the_review_command():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == EXIT_OK
    assert "review" in result.output


def test_review_help_documents_the_scope_flags():
    result = runner.invoke(app, ["review", "--help"])
    assert result.exit_code == EXIT_OK
    for flag in ("--base", "--uncommitted", "--committed", "--no-llm", "--fail-on"):
        assert flag in result.output


def test_no_llm_on_a_clean_tree_reports_nothing(repo: Path):
    result = runner.invoke(app, ["review", "--no-llm", "--uncommitted", "-C", str(repo)])
    assert result.exit_code == EXIT_OK
    assert "No changes to review" in result.output


def test_no_llm_with_changes_exits_clean(repo: Path):
    (repo / "app.py").write_text("def f():\n    return 2\n")
    result = runner.invoke(app, ["review", "--no-llm", "--uncommitted", "-C", str(repo)])
    assert result.exit_code == EXIT_OK
    assert "No findings" in result.output


def test_not_a_repo_is_an_error(tmp_path: Path):
    plain = tmp_path / "plain"
    plain.mkdir()
    result = runner.invoke(app, ["review", "--no-llm", "-C", str(plain)])
    assert result.exit_code == EXIT_ERROR
    assert "not a git repository" in result.output


def test_missing_credentials_is_reported_before_any_call(repo: Path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    result = runner.invoke(
        app, ["review", "--uncommitted", "-C", str(repo), "-m", "anthropic/claude-sonnet-5"]
    )
    assert result.exit_code == EXIT_ERROR
    assert "ANTHROPIC_API_KEY" in result.output


def test_conflicting_scope_flags_are_rejected(repo: Path):
    result = runner.invoke(
        app, ["review", "--committed", "--uncommitted", "--no-llm", "-C", str(repo)]
    )
    assert result.exit_code == EXIT_ERROR
    assert "mutually exclusive" in result.output


def test_bad_config_is_reported(repo: Path):
    (repo / ".roborak.yaml").write_text("llm:\n  temperature: not-a-number\n")
    result = runner.invoke(app, ["review", "--no-llm", "-C", str(repo)])
    assert result.exit_code == EXIT_ERROR


def test_missing_config_file_is_reported(repo: Path):
    result = runner.invoke(
        app, ["review", "--no-llm", "-C", str(repo), "--config", str(repo / "nope.yaml")]
    )
    assert result.exit_code == EXIT_ERROR


def test_fail_on_gates_the_exit_code(repo: Path, monkeypatch):
    """``--fail-on`` is what makes roborak usable as a CI gate."""
    from roborak.core.models import Finding, ReviewResult
    from roborak.core.severity import Category, Severity

    def fake_review(self, changeset):
        return ReviewResult(
            changeset=changeset,
            findings=[
                Finding(
                    file="app.py",
                    start_line=1,
                    end_line=1,
                    severity=Severity.CRITICAL,
                    category=Category.SECURITY,
                    title="Boom",
                    body="A critical problem.",
                )
            ],
        )

    monkeypatch.setattr("roborak.analysis.reviewer.Reviewer.review", fake_review)
    (repo / "app.py").write_text("def f():\n    return 2\n")

    gated = runner.invoke(
        app, ["review", "--no-llm", "--uncommitted", "-C", str(repo), "--fail-on", "major"]
    )
    assert gated.exit_code == EXIT_FINDINGS

    ungated = runner.invoke(app, ["review", "--no-llm", "--uncommitted", "-C", str(repo)])
    assert ungated.exit_code == EXIT_OK


def test_cli_flags_beat_the_config_file(repo: Path, monkeypatch):
    (repo / ".roborak.yaml").write_text("llm:\n  model: file/model\n")
    seen: dict[str, str] = {}

    def capture(model: str) -> str | None:
        seen["model"] = model
        return "SOME_KEY"  # short-circuit before any provider call

    monkeypatch.setattr("roborak.cli.commands.review.missing_credentials", capture)
    (repo / "app.py").write_text("def f():\n    return 2\n")

    runner.invoke(app, ["review", "--uncommitted", "-C", str(repo), "-m", "flag/model"])
    assert seen["model"] == "flag/model"
