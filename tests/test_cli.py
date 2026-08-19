"""CLI surface tests, driven through Typer's runner."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from roborak.cli.main import app
from roborak.cli.shared import EXIT_ERROR, EXIT_FINDINGS, EXIT_OK

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

    monkeypatch.setattr("roborak.cli.shared.missing_credentials", capture)
    (repo / "app.py").write_text("def f():\n    return 2\n")

    runner.invoke(app, ["review", "--uncommitted", "-C", str(repo), "-m", "flag/model"])
    assert seen["model"] == "flag/model"


# -- output modes ----------------------------------------------------------


def test_json_mode_emits_only_json(repo: Path):
    """Anything else on stdout would break whatever is parsing it."""
    (repo / "app.py").write_text("def f():\n    return 2\n")
    result = runner.invoke(app, ["review", "--no-llm", "--uncommitted", "-C", str(repo), "--json"])
    assert result.exit_code == EXIT_OK
    payload = json.loads(result.stdout)
    assert payload["schema_version"] == 1
    assert "findings" in payload


def test_agent_mode_emits_only_json(repo: Path):
    (repo / "app.py").write_text("def f():\n    return 2\n")
    result = runner.invoke(app, ["review", "--no-llm", "--uncommitted", "-C", str(repo), "--agent"])
    assert result.exit_code == EXIT_OK
    assert set(json.loads(result.stdout)) == {"schema_version", "summary", "findings"}


def test_prompt_only_mode(repo: Path):
    (repo / "app.py").write_text("def f():\n    return 2\n")
    result = runner.invoke(
        app, ["review", "--no-llm", "--uncommitted", "-C", str(repo), "--prompt-only"]
    )
    assert result.exit_code == EXIT_OK
    assert "No findings." in result.stdout


def test_markdown_report_is_written(repo: Path, tmp_path: Path):
    (repo / "app.py").write_text("def f():\n    return 2\n")
    out = tmp_path / "report.md"
    result = runner.invoke(
        app,
        ["review", "--no-llm", "--uncommitted", "-C", str(repo), "--markdown", str(out)],
    )
    assert result.exit_code == EXIT_OK
    assert out.is_file()
    assert out.read_text().startswith("#")


# -- forge flags -----------------------------------------------------------


def test_mr_and_pr_are_mutually_exclusive(repo: Path):
    result = runner.invoke(app, ["review", "--no-llm", "-C", str(repo), "--mr", "1", "--pr", "2"])
    assert result.exit_code == EXIT_ERROR
    assert "mutually exclusive" in result.output


def test_post_without_a_forge_target_is_refused(repo: Path):
    result = runner.invoke(app, ["review", "--no-llm", "-C", str(repo), "--post"])
    assert result.exit_code == EXIT_ERROR
    assert "nowhere to post" in result.output


def test_missing_forge_token_is_reported(repo: Path, monkeypatch):
    for name in ("GITLAB_TOKEN", "ROBORAK_GITLAB_TOKEN", "CI_JOB_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    result = runner.invoke(app, ["review", "--no-llm", "-C", str(repo), "--mr", "298"])
    assert result.exit_code == EXIT_ERROR
    assert "GITLAB_TOKEN" in result.output


def test_unparseable_mr_reference_is_reported(repo: Path, monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN", "tok")
    result = runner.invoke(app, ["review", "--no-llm", "-C", str(repo), "--mr", "nonsense"])
    assert result.exit_code == EXIT_ERROR


# -- other commands --------------------------------------------------------


@pytest.mark.parametrize("command", ["describe", "improve", "ask"])
def test_every_command_has_help(command):
    result = runner.invoke(app, [command, "--help"])
    assert result.exit_code == EXIT_OK


def test_commands_are_registered():
    result = runner.invoke(app, ["--help"])
    for command in ("review", "describe", "improve", "ask"):
        assert command in result.output


def test_ask_requires_a_question():
    result = runner.invoke(app, ["ask"])
    assert result.exit_code != EXIT_OK


# -- rules and config commands --------------------------------------------


def test_rules_init_list_and_test_round_trip(repo: Path):
    created = runner.invoke(app, ["rules", "init", "-C", str(repo)])
    assert created.exit_code == EXIT_OK

    listed = runner.invoke(app, ["rules", "list", "-C", str(repo)])
    assert listed.exit_code == EXIT_OK
    assert "no-raw-sql" in listed.output

    rule_file = repo / ".roborak" / "rules" / "no-raw-sql.md"
    checked = runner.invoke(app, ["rules", "test", str(rule_file), "app/svc.py"])
    assert checked.exit_code == EXIT_OK
    assert "parses cleanly" in checked.output
    assert "applies to" in checked.output


def test_rules_init_refuses_to_overwrite(repo: Path):
    runner.invoke(app, ["rules", "init", "-C", str(repo)])
    second = runner.invoke(app, ["rules", "init", "-C", str(repo)])
    assert second.exit_code == EXIT_ERROR
    assert "not overwriting" in second.output


def test_rules_list_with_no_rules(repo: Path):
    result = runner.invoke(app, ["rules", "list", "-C", str(repo)])
    assert result.exit_code == EXIT_OK
    assert "No rules found" in result.output


def test_rules_test_reports_a_broken_rule(repo: Path, tmp_path: Path):
    bad = tmp_path / "bad.md"
    bad.write_text("---\n- not a mapping\n---\nBody.")
    result = runner.invoke(app, ["rules", "test", str(bad)])
    assert result.exit_code == EXIT_ERROR


def test_config_init_and_show(repo: Path):
    created = runner.invoke(app, ["config", "init", "-C", str(repo)])
    assert created.exit_code == EXIT_OK
    assert (repo / ".roborak.yaml").is_file()

    shown = runner.invoke(app, ["config", "show", "-C", str(repo)])
    assert shown.exit_code == EXIT_OK
    assert "severity_floor" in shown.output


def test_config_init_refuses_to_overwrite_without_force(repo: Path):
    runner.invoke(app, ["config", "init", "-C", str(repo)])
    second = runner.invoke(app, ["config", "init", "-C", str(repo)])
    assert second.exit_code == EXIT_ERROR

    forced = runner.invoke(app, ["config", "init", "-C", str(repo), "--force"])
    assert forced.exit_code == EXIT_OK
