"""CLI surface tests, driven through Typer's runner."""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from roborak import __version__
from roborak.cli.main import app
from roborak.cli.shared import EXIT_ERROR, EXIT_FINDINGS, EXIT_OK
from roborak.publish.base import RemoteState

runner = CliRunner()


_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def flatten(output: str) -> str:
    """Rich styles and wraps CLI output; assert against the flattened text.

    Typer colours ``--help`` under GITHUB_ACTIONS and highlights the leading dash
    of a switch separately, so a literal ``--base`` is never in the raw output.
    """
    return " ".join(_ANSI.sub("", output).split())


def unwrapped(output: str) -> str:
    """Rich hard-wraps at the terminal width, splitting long paths mid-token."""
    return output.replace("\n", "")


@pytest.fixture
def repo(tmp_path: Path, monkeypatch) -> Path:
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
    assert "review" in flatten(result.output)


def test_version_reports_the_installed_version():
    """Eager, so it prints and exits instead of falling through to a bare ``review``."""
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == EXIT_OK
    assert flatten(result.output) == f"roborak {__version__}"


def test_review_help_documents_the_scope_flags():
    result = runner.invoke(app, ["review", "--help"])
    assert result.exit_code == EXIT_OK
    help_text = flatten(result.output)
    for flag in ("--base", "--uncommitted", "--committed", "--no-llm", "--fail-on"):
        assert flag in help_text


def test_no_llm_on_a_clean_tree_reports_nothing(repo: Path):
    result = runner.invoke(app, ["review", "--no-llm", "--uncommitted", "-C", str(repo)])
    assert result.exit_code == EXIT_OK
    assert "No changes to review" in flatten(result.output)


def test_no_llm_with_changes_exits_clean(repo: Path):
    (repo / "app.py").write_text("def f():\n    return 2\n")
    result = runner.invoke(app, ["review", "--no-llm", "--uncommitted", "-C", str(repo)])
    assert result.exit_code == EXIT_OK
    assert "No findings" in flatten(result.output)


def test_a_plain_directory_is_reviewed_file_by_file(tmp_path: Path):
    """The point of the fallback: no repository, no baseline, still a review."""
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "app.py").write_text("def f():\n    return 2\n")
    result = runner.invoke(app, ["review", "--no-llm", "--json", "-C", str(plain)])
    assert result.exit_code == EXIT_OK
    assert json.loads(result.stdout)["changeset"]["origin"] == "paths"


def test_a_missing_directory_is_reported_as_a_source_error(tmp_path: Path):
    missing = tmp_path / "missing"
    result = runner.invoke(app, ["review", "--no-llm", "-C", str(missing)])
    assert result.exit_code == EXIT_ERROR
    assert "does not exist" in flatten(result.output)


def test_a_git_repository_still_reviews_the_diff(repo: Path):
    """The fallback must not capture the case it was never meant to."""
    (repo / "app.py").write_text("def f():\n    return 2\n")
    result = runner.invoke(app, ["review", "--no-llm", "--json", "-C", str(repo)])
    assert result.exit_code == EXIT_OK
    assert json.loads(result.stdout)["changeset"]["origin"] == "local"


def test_git_only_flags_are_refused_outside_a_repository(tmp_path: Path):
    plain = tmp_path / "plain"
    plain.mkdir()
    result = runner.invoke(app, ["review", "--no-llm", "--uncommitted", "-C", str(plain)])
    assert result.exit_code == EXIT_ERROR
    assert "not a git repository" in flatten(result.output)


def test_missing_credentials_is_reported_before_any_call(repo: Path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    result = runner.invoke(
        app, ["review", "--uncommitted", "-C", str(repo), "-m", "anthropic/claude-sonnet-5"]
    )
    assert result.exit_code == EXIT_ERROR
    assert "ANTHROPIC_API_KEY" in flatten(result.output)


def test_conflicting_scope_flags_are_rejected(repo: Path):
    result = runner.invoke(
        app, ["review", "--committed", "--uncommitted", "--no-llm", "-C", str(repo)]
    )
    assert result.exit_code == EXIT_ERROR
    assert "mutually exclusive" in flatten(result.output)


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

    def capture(model: str, llm=None) -> str | None:
        seen["model"] = model
        return "SOME_KEY"

    monkeypatch.setattr("roborak.cli.shared.missing_credentials", capture)
    (repo / "app.py").write_text("def f():\n    return 2\n")

    runner.invoke(app, ["review", "--uncommitted", "-C", str(repo), "-m", "flag/model"])
    assert seen["model"] == "flag/model"


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
    assert set(json.loads(result.stdout)) == {
        "schema_version",
        "status",
        "errors",
        "coverage",
        "summary",
        "findings",
    }


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
    assert out.read_text(encoding="utf-8").startswith("#")


def test_mr_and_pr_are_mutually_exclusive(repo: Path):
    result = runner.invoke(app, ["review", "--no-llm", "-C", str(repo), "--mr", "1", "--pr", "2"])
    assert result.exit_code == EXIT_ERROR
    assert "mutually exclusive" in flatten(result.output)


def test_post_without_a_forge_target_is_refused(repo: Path):
    result = runner.invoke(app, ["review", "--no-llm", "-C", str(repo), "--post"])
    assert result.exit_code == EXIT_ERROR
    assert "nowhere to post" in flatten(result.output)


def test_missing_forge_token_is_reported(repo: Path, monkeypatch):
    for name in ("GITLAB_TOKEN", "ROBORAK_GITLAB_TOKEN", "CI_JOB_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    result = runner.invoke(app, ["review", "--no-llm", "-C", str(repo), "--mr", "298"])
    assert result.exit_code == EXIT_ERROR
    assert "GITLAB_TOKEN" in flatten(result.output)
    assert "forge.tokens.gitlab" in flatten(result.output)


def test_config_show_reports_a_configured_forge_host(repo: Path):
    (repo / ".roborak.yaml").write_text("forge:\n  hosts:\n    gitlab: gitlab.acme.com\n")
    result = runner.invoke(app, ["config", "show", "-C", str(repo)])
    assert result.exit_code == EXIT_OK
    assert "gitlab.acme.com" in flatten(result.output)


def test_a_configured_host_names_the_forge_for_issue_lookup(repo: Path, monkeypatch):
    for name in ("GITLAB_TOKEN", "ROBORAK_GITLAB_TOKEN", "CI_JOB_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    subprocess.run(
        ["git", "remote", "add", "origin", "git@git.corp.example:acme/app.git"],
        cwd=repo,
        check=True,
    )
    unconfigured = runner.invoke(app, ["review", "--no-llm", "-C", str(repo), "--issue", "24"])
    assert unconfigured.exit_code == EXIT_ERROR
    assert "Could not tell which forge" in flatten(unconfigured.output)

    (repo / ".roborak.yaml").write_text("forge:\n  hosts:\n    gitlab: git.corp.example\n")
    result = runner.invoke(app, ["review", "--no-llm", "-C", str(repo), "--issue", "24"])
    assert result.exit_code == EXIT_ERROR
    assert "needs a gitlab token" in flatten(result.output)


def test_config_show_redacts_a_forge_token(repo: Path):
    (repo / ".roborak.yaml").write_text("forge:\n  tokens:\n    gitlab: glpat-secret\n")
    result = runner.invoke(app, ["config", "show", "-C", str(repo)])
    assert result.exit_code == EXIT_OK
    assert "glpat-secret" not in flatten(result.output)
    assert "gitlab" in flatten(result.output)


def test_unparseable_mr_reference_is_reported(repo: Path, monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN", "tok")
    result = runner.invoke(app, ["review", "--no-llm", "-C", str(repo), "--mr", "nonsense"])
    assert result.exit_code == EXIT_ERROR


@pytest.mark.parametrize("command", ["describe", "improve", "ask"])
def test_every_command_has_help(command):
    result = runner.invoke(app, [command, "--help"])
    assert result.exit_code == EXIT_OK


def test_commands_are_registered():
    result = runner.invoke(app, ["--help"])
    for command in ("review", "describe", "improve", "ask"):
        assert command in flatten(result.output)


def test_ask_requires_a_question():
    result = runner.invoke(app, ["ask"])
    assert result.exit_code != EXIT_OK


def test_rules_init_list_and_test_round_trip(repo: Path):
    created = runner.invoke(app, ["rules", "init", "-C", str(repo)])
    assert created.exit_code == EXIT_OK

    listed = runner.invoke(app, ["rules", "list", "-C", str(repo)])
    assert listed.exit_code == EXIT_OK
    assert "no-raw-sql" in flatten(listed.output)

    rule_file = repo / ".roborak" / "rules" / "no-raw-sql.md"
    checked = runner.invoke(app, ["rules", "test", str(rule_file), "app/svc.py"])
    assert checked.exit_code == EXIT_OK
    assert "parses cleanly" in flatten(checked.output)
    assert "applies to" in flatten(checked.output)


def test_rules_init_refuses_to_overwrite(repo: Path):
    runner.invoke(app, ["rules", "init", "-C", str(repo)])
    second = runner.invoke(app, ["rules", "init", "-C", str(repo)])
    assert second.exit_code == EXIT_ERROR
    assert "not overwriting" in flatten(second.output)


def test_rules_list_with_no_rules(repo: Path):
    result = runner.invoke(app, ["rules", "list", "-C", str(repo)])
    assert result.exit_code == EXIT_OK
    assert "No rules found" in flatten(result.output)


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
    assert "severity_floor" in flatten(shown.output)


def test_config_init_refuses_to_overwrite_without_force(repo: Path):
    runner.invoke(app, ["config", "init", "-C", str(repo)])
    second = runner.invoke(app, ["config", "init", "-C", str(repo)])
    assert second.exit_code == EXIT_ERROR

    forced = runner.invoke(app, ["config", "init", "-C", str(repo), "--force"])
    assert forced.exit_code == EXIT_OK


@pytest.mark.parametrize("command", ["review", "describe", "improve", "ask"])
def test_issue_flag_is_offered_by_every_command(command):
    result = runner.invoke(app, [command, "--help"])
    assert result.exit_code == EXIT_OK
    assert "--issue" in flatten(result.output)


@pytest.mark.parametrize("command", ["review", "describe", "improve", "ask"])
def test_discussion_opt_out_is_offered_by_every_change_command(command):
    result = runner.invoke(app, [command, "--help"])
    assert result.exit_code == EXIT_OK
    assert "--no-discussions" in flatten(result.output)


def test_issue_without_a_recognisable_forge_fails_clearly(repo: Path, monkeypatch):
    monkeypatch.setattr("roborak.cli.shared.detect_provider", lambda *a, **k: None)
    result = runner.invoke(app, ["review", "--no-llm", "--issue", "42", "-C", str(repo)])
    assert result.exit_code == EXIT_ERROR
    assert "full issue URL" in flatten(result.output)


def test_issue_without_a_token_fails_before_any_fetch(repo: Path, monkeypatch):
    monkeypatch.setattr("roborak.cli.shared.get_token", lambda provider, forge=None: None)
    result = runner.invoke(
        app,
        [
            "review",
            "--no-llm",
            "--issue",
            "https://github.com/acme/web/issues/42",
            "-C",
            str(repo),
        ],
    )
    assert result.exit_code == EXIT_ERROR
    assert "GITHUB_TOKEN" in flatten(result.output)


def _stub_issue():
    from roborak.core.models import Issue

    return Issue(
        provider="github",
        host="github.com",
        project="acme/web",
        number=42,
        title="Sessions can be hijacked",
    )


def test_a_linked_pull_request_becomes_the_review_target(repo: Path, monkeypatch):
    from roborak.core.models import ChangeSet
    from roborak.sources.issue import LinkedChange

    monkeypatch.setattr("roborak.cli.shared.get_token", lambda provider, forge=None: "tok")
    monkeypatch.setattr("roborak.cli.shared.load_issue", lambda t, tok: _stub_issue())
    monkeypatch.setattr(
        "roborak.cli.shared.resolve_linked_change",
        lambda t, tok: LinkedChange(number=57, state="open"),
    )

    seen: dict[str, object] = {}

    class FakeSource:
        def __init__(self, target, token):
            seen["number"] = target.number

        def load(self):
            return ChangeSet(origin="github", title="from the pull request")

    monkeypatch.setattr("roborak.cli.shared.GitHubSource", FakeSource)

    result = runner.invoke(
        app,
        [
            "review",
            "--no-llm",
            "--issue",
            "https://github.com/acme/web/issues/42",
            "-C",
            str(repo),
        ],
    )
    assert result.exit_code == EXIT_OK, result.output
    assert seen["number"] == 57
    assert "pull request #57" in flatten(result.output)


def test_no_linked_change_falls_back_to_the_local_diff(repo: Path, monkeypatch):
    monkeypatch.setattr("roborak.cli.shared.get_token", lambda provider, forge=None: "tok")
    monkeypatch.setattr("roborak.cli.shared.load_issue", lambda t, tok: _stub_issue())
    monkeypatch.setattr("roborak.cli.shared.resolve_linked_change", lambda t, tok: None)

    (repo / "app.py").write_text("def f():\n    return 2\n")
    result = runner.invoke(
        app,
        [
            "review",
            "--no-llm",
            "--issue",
            "https://github.com/acme/web/issues/42",
            "-C",
            str(repo),
        ],
    )
    assert result.exit_code == EXIT_OK, result.output
    assert "No findings" in flatten(result.output)
    assert "against #42" in flatten(result.output)


def test_a_stated_target_stops_the_issue_choosing_one(repo: Path, monkeypatch):
    monkeypatch.setattr("roborak.cli.shared.get_token", lambda provider, forge=None: "tok")
    monkeypatch.setattr("roborak.cli.shared.load_issue", lambda t, tok: _stub_issue())

    def refuse(*a, **k):
        raise AssertionError("--base names the target; the issue must not override it")

    monkeypatch.setattr("roborak.cli.shared.resolve_linked_change", refuse)

    result = runner.invoke(
        app,
        [
            "review",
            "--no-llm",
            "--base",
            "main",
            "--issue",
            "https://github.com/acme/web/issues/42",
            "-C",
            str(repo),
        ],
    )
    assert result.exit_code == EXIT_OK, result.output


def test_post_with_an_unlinked_issue_is_refused(repo: Path, monkeypatch):
    monkeypatch.setattr("roborak.cli.shared.get_token", lambda provider, forge=None: "tok")
    monkeypatch.setattr("roborak.cli.shared.load_issue", lambda t, tok: _stub_issue())
    monkeypatch.setattr("roborak.cli.shared.resolve_linked_change", lambda t, tok: None)

    result = runner.invoke(
        app,
        [
            "review",
            "--no-llm",
            "--post",
            "--issue",
            "https://github.com/acme/web/issues/42",
            "-C",
            str(repo),
        ],
    )
    assert result.exit_code == EXIT_ERROR
    assert "no linked change" in flatten(result.output)


@pytest.fixture
def user_config(tmp_path: Path, monkeypatch) -> Path:
    """Point the user-wide config somewhere disposable, for both readers of it."""
    path = tmp_path / "home" / ".config" / "roborak" / "config.yaml"
    monkeypatch.setattr("roborak.core.config.USER_CONFIG_PATH", path)
    return path


def test_global_init_writes_the_user_config(user_config: Path):
    result = runner.invoke(app, ["config", "init", "--global"])
    assert result.exit_code == EXIT_OK, result.output
    assert user_config.is_file()
    assert user_config.parent.is_dir()


def test_global_init_is_not_world_readable(user_config: Path):
    runner.invoke(app, ["config", "init", "--global"])
    if os.name != "nt":  # Windows synthesises st_mode; the bits mean nothing there
        assert user_config.stat().st_mode & 0o077 == 0


def test_global_init_refuses_to_overwrite_without_force(user_config: Path):
    assert runner.invoke(app, ["config", "init", "--global"]).exit_code == EXIT_OK
    user_config.write_text("version: 1\n# hand-edited\n")

    second = runner.invoke(app, ["config", "init", "--global"])
    assert second.exit_code == EXIT_ERROR
    assert "# hand-edited" in user_config.read_text(encoding="utf-8"), (
        "must not clobber a real config"
    )

    assert runner.invoke(app, ["config", "init", "--global", "--force"]).exit_code == EXIT_OK
    assert "# hand-edited" not in user_config.read_text(encoding="utf-8")


def test_global_and_dir_together_are_refused(repo: Path, user_config: Path):
    result = runner.invoke(app, ["config", "init", "--global", "-C", str(repo)])
    assert result.exit_code == EXIT_ERROR
    assert not user_config.exists()
    assert not (repo / ".roborak.yaml").exists()


def test_global_init_is_picked_up_by_show(repo: Path, user_config: Path):
    runner.invoke(app, ["config", "init", "--global"])
    shown = runner.invoke(app, ["config", "show", "-C", str(repo)])
    assert shown.exit_code == EXIT_OK
    assert str(user_config) in unwrapped(shown.output)


def test_the_scaffolded_file_is_the_commented_template(repo: Path):
    runner.invoke(app, ["config", "init", "-C", str(repo)])
    written = (repo / ".roborak.yaml").read_text(encoding="utf-8")

    assert written.startswith("# roborak configuration")
    assert "check_requirements" in written
    assert "include_discussions" in written
    assert "# null autodetects whatever is on PATH." in written


def test_the_template_ships_inside_the_package():
    from importlib import resources

    from roborak.cli.commands.config_cmd import TEMPLATE_NAME, template_text

    assert (resources.files("roborak") / TEMPLATE_NAME).is_file()
    assert template_text().startswith("# roborak configuration")


def test_config_show_names_an_explicit_config_file(repo: Path, tmp_path: Path):
    explicit = tmp_path / "elsewhere.yaml"
    explicit.write_text("version: 1\n")
    shown = runner.invoke(app, ["config", "show", "-C", str(repo), "--config", str(explicit)])
    assert shown.exit_code == EXIT_OK
    assert str(explicit) in unwrapped(shown.output)


@pytest.fixture
def wizard(monkeypatch):
    """Pin the wizard to its line-based path, and keep the real environment out.

    CliRunner already fails the tty test, so this only makes explicit what these
    tests rely on: typed answers fed as a flat string, no arrow keys involved.
    """
    monkeypatch.setattr("roborak.cli.commands.setup_cmd._is_interactive", lambda: False)
    monkeypatch.setattr("roborak.cli.commands.setup_cmd.get_token", lambda *a, **k: None)
    for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GITLAB_TOKEN", "GITHUB_TOKEN"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def picker(monkeypatch, wizard):
    """The selector path: a terminal, with the arrow-key lists answered for us.

    ``_select`` is the seam. Patching it keeps the test off a pty while still
    driving the real branch, the same way the tty predicates are patched
    elsewhere in this file.
    """
    monkeypatch.setattr("roborak.cli.commands.setup_cmd._is_interactive", lambda: True)
    answers: list[str] = []

    def choose(label, choices):
        assert answers, f"no canned answer left for {label!r}"
        return answers.pop(0)

    monkeypatch.setattr("roborak.cli.commands.setup_cmd._select", choose)
    return answers


def test_setup_writes_a_config_that_round_trips(wizard, repo: Path, user_config: Path):
    from roborak.core.config import load_config

    result = runner.invoke(
        app,
        ["setup"],
        input="1\nanthropic/claude-opus-5\nsk-ant-secret\n\n\n",
    )
    assert result.exit_code == EXIT_OK, result.output
    assert user_config.is_file()

    config = load_config(repo)
    assert config.llm.model == "anthropic/claude-opus-5"
    assert config.llm.api_keys["anthropic"].get_secret_value() == "sk-ant-secret"


def test_setup_writes_only_the_answered_keys(wizard, user_config: Path):
    runner.invoke(app, ["setup"], input="1\n\nsk-ant-secret\n\n\n")
    written = yaml.safe_load(user_config.read_text(encoding="utf-8"))

    assert set(written) == {"version", "llm"}
    assert set(written["llm"]) == {"model", "api_keys"}


def test_setup_skips_the_key_when_the_environment_has_one(wizard, monkeypatch, user_config: Path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-from-env")
    result = runner.invoke(app, ["setup"], input="1\n\n\n\n")
    assert result.exit_code == EXIT_OK, result.output
    assert "api_keys" not in yaml.safe_load(user_config.read_text(encoding="utf-8"))["llm"]


def test_setup_secures_the_user_config(wizard, user_config: Path):
    runner.invoke(app, ["setup"], input="1\n\nsk-ant-secret\n\n\n")
    if os.name != "nt":  # Windows synthesises st_mode; the bits mean nothing there
        assert user_config.stat().st_mode & 0o077 == 0


def test_setup_secures_a_project_config_too(wizard, repo: Path, user_config: Path):
    """`config init` keys the mode off --global; here it must follow the content."""
    result = runner.invoke(app, ["setup", "-C", str(repo)], input="2\n\nsk-ant-secret\n\n\n")
    assert result.exit_code == EXIT_OK, result.output
    written = repo / ".roborak.yaml"
    assert written.is_file()
    if os.name != "nt":  # Windows synthesises st_mode; the bits mean nothing there
        assert written.stat().st_mode & 0o077 == 0


def test_setup_warns_when_a_secret_file_is_not_gitignored(wizard, repo: Path, user_config: Path):
    result = runner.invoke(app, ["setup", "-C", str(repo)], input="2\n\nsk-ant-secret\n\n\n")
    assert "git does not ignore it" in flatten(result.output)

    (repo / ".roborak.yaml").unlink()
    (repo / ".gitignore").write_text(".roborak.yaml\n")
    second = runner.invoke(app, ["setup", "-C", str(repo)], input="2\n\nsk-ant-secret\n\n\n")
    assert "git does not ignore it" not in flatten(second.output)


def test_setup_refuses_to_overwrite_without_force(wizard, user_config: Path):
    assert runner.invoke(app, ["setup"], input="1\n\nsk-ant\n\n\n").exit_code == EXIT_OK
    user_config.write_text("version: 1\n# hand-edited\n")

    second = runner.invoke(app, ["setup"], input="1\n\nsk-ant\n\n\n")
    assert second.exit_code == EXIT_ERROR
    assert "# hand-edited" in user_config.read_text(encoding="utf-8"), (
        "must not clobber a real config"
    )

    forced = runner.invoke(app, ["setup", "--force"], input="1\n\nsk-ant\n\n\n")
    assert forced.exit_code == EXIT_OK
    assert "# hand-edited" not in user_config.read_text(encoding="utf-8")


def test_setup_aborts_without_writing_anything(wizard, user_config: Path):
    """Input running out is an EOF mid-wizard, the same event as a Ctrl-C."""
    result = runner.invoke(app, ["setup"], input="1\n")
    assert result.exit_code == EXIT_OK
    assert "aborted" in flatten(result.output)
    assert not user_config.exists()


def test_setup_normalises_a_self_hosted_host(wizard, user_config: Path):
    runner.invoke(
        app,
        ["setup"],
        input="1\n\nsk-ant\nglpat-x\nhttps://gitlab.acme.com/\n\n",
    )
    written = yaml.safe_load(user_config.read_text(encoding="utf-8"))
    assert written["forge"]["hosts"]["gitlab"] == "gitlab.acme.com"
    assert written["forge"]["tokens"]["gitlab"] == "glpat-x"


def test_setup_reprompts_on_a_host_that_is_a_url_path(wizard, user_config: Path):
    result = runner.invoke(
        app,
        ["setup"],
        input="1\n\nsk-ant\nglpat-x\ngitlab.acme.com/group\ngitlab.acme.com\n\n",
    )
    assert result.exit_code == EXIT_OK, result.output
    assert "must be a domain" in flatten(result.output)
    hosts = yaml.safe_load(user_config.read_text(encoding="utf-8"))["forge"]["hosts"]
    assert hosts["gitlab"] == "gitlab.acme.com"


def test_setup_without_a_terminal_exits_instead_of_hanging(user_config: Path):
    """CliRunner fails the tty test naturally, exactly as a CI runner does.

    Nothing on stdin means the first question hits EOF, so it aborts rather than
    waiting for an answer nobody is there to give.
    """
    result = runner.invoke(app, ["setup"], input="")
    assert result.exit_code == EXIT_OK
    assert "aborted" in flatten(result.output)
    assert "rk config init" in flatten(result.output)
    assert not user_config.exists()


def test_setup_without_a_terminal_still_answers_from_a_pipe(user_config: Path, monkeypatch):
    """A non-tty is no longer fatal: piped answers drive the line-based path."""
    monkeypatch.setattr("roborak.cli.commands.setup_cmd.get_token", lambda *a, **k: None)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    result = runner.invoke(app, ["setup"], input="1\n\nsk-ant-piped\n\n\n")
    assert result.exit_code == EXIT_OK, result.output
    written = yaml.safe_load(user_config.read_text(encoding="utf-8"))
    assert written["llm"]["api_keys"]["anthropic"] == "sk-ant-piped"


def test_setup_picks_the_destination_from_a_list(picker, repo: Path, user_config: Path):
    """No number typing: the choice is the value, and the input is only free text."""
    picker.extend(["project", "anthropic/claude-opus-5"])
    result = runner.invoke(app, ["setup", "-C", str(repo)], input="sk-ant\n\n\n")

    assert result.exit_code == EXIT_OK, result.output
    assert not user_config.exists()
    written = yaml.safe_load((repo / ".roborak.yaml").read_text(encoding="utf-8"))
    assert written["llm"]["model"] == "anthropic/claude-opus-5"


def test_setup_picks_a_known_model_without_typing_it(picker, user_config: Path):
    picker.extend(["user", "gemini/gemini-2.5-pro"])
    result = runner.invoke(app, ["setup"], input="sk-gemini\n\n\n")

    assert result.exit_code == EXIT_OK, result.output
    written = yaml.safe_load(user_config.read_text(encoding="utf-8"))
    assert written["llm"]["model"] == "gemini/gemini-2.5-pro"


def test_setup_falls_through_to_free_text_on_other(picker, repo: Path, user_config: Path):
    """A curated list is not a ceiling -- `Other` lands in the typed prompt."""
    from roborak.cli.commands.setup_cmd import OTHER

    picker.extend([OTHER, OTHER])
    result = runner.invoke(
        app,
        ["setup", "-C", str(repo)],
        input=f"{repo / 'typed.yaml'}\nollama/llama3\n\n\n",
    )

    assert result.exit_code == EXIT_OK, result.output
    written = yaml.safe_load((repo / "typed.yaml").read_text(encoding="utf-8"))
    assert written["llm"]["model"] == "ollama/llama3"


def test_setup_aborts_when_the_selector_is_cancelled(picker, monkeypatch, user_config: Path):
    """Ctrl-C in a list is the same event as an EOF in a prompt."""
    from roborak.cli.commands import setup_cmd

    def cancel(label, choices):
        raise setup_cmd.Aborted

    monkeypatch.setattr(setup_cmd, "_select", cancel)
    result = runner.invoke(app, ["setup"], input="")

    assert result.exit_code == EXIT_OK
    assert "aborted" in flatten(result.output)
    assert not user_config.exists()


def test_select_appends_the_escape_hatch_and_maps_both_exits_to_abort(monkeypatch):
    """The two things `_select` owns, tested without a terminal."""
    import questionary

    from roborak.cli.commands.setup_cmd import OTHER, Aborted, _select

    seen: dict[str, object] = {}

    class FakeQuestion:
        def __init__(self, answer):
            self._answer = answer

        def ask(self):
            if isinstance(self._answer, BaseException):
                raise self._answer
            return self._answer

    def fake_select(label, choices, **kwargs):
        seen["titles"] = [choice.title for choice in choices]
        return FakeQuestion(seen["answer"])

    monkeypatch.setattr(questionary, "select", fake_select)

    seen["answer"] = "user"
    assert _select("Where?", [questionary.Choice(title="~/x", value="user")]) == "user"
    assert seen["titles"][-1] == "Other (type it in)…"

    seen["answer"] = OTHER
    assert _select("Where?", [questionary.Choice(title="~/x", value="user")]) == OTHER

    # Ctrl-C: questionary swallows it and hands back None.
    seen["answer"] = None
    with pytest.raises(Aborted):
        _select("Where?", [questionary.Choice(title="~/x", value="user")])

    # A stdin that ends: prompt_toolkit raises, and questionary does not catch it.
    seen["answer"] = EOFError()
    with pytest.raises(Aborted):
        _select("Where?", [questionary.Choice(title="~/x", value="user")])


def test_help_lists_the_setup_command():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == EXIT_OK
    assert "setup" in flatten(result.output)


def test_the_new_flags_are_documented():
    result = runner.invoke(app, ["review", "--help"])
    assert result.exit_code == EXIT_OK
    help_text = flatten(result.output)
    for flag in ("--no-post", "--no-walkthrough", "--full", "--panels"):
        assert flag in help_text


def test_a_non_tty_is_never_prompted(repo: Path):
    """CliRunner and every CI runner fail the tty test, so the run must not block."""
    (repo / "app.py").write_text("def f():\n    return 2\n")
    result = runner.invoke(app, ["review", "--no-llm", "--uncommitted", "-C", str(repo)])
    assert result.exit_code == EXIT_OK
    assert "Save this review" not in flatten(result.output)


def _make_interactive(monkeypatch):
    monkeypatch.setattr("roborak.cli.commands.review._is_interactive", lambda: True)


def _with_one_finding(monkeypatch):
    """Give the run something worth sharing; a clean review is never offered."""
    from roborak.analysis.reviewer import Reviewer
    from roborak.core.models import Finding, ReviewResult
    from roborak.core.severity import Category, Severity

    def fake_review(self, changeset):
        return ReviewResult(
            changeset=changeset,
            findings=[
                Finding(
                    file="app.py",
                    start_line=2,
                    end_line=2,
                    severity=Severity.MAJOR,
                    category=Category.BUG,
                    title="Returns the wrong value",
                    body="f() now returns 2 where callers expect 1.",
                )
            ],
        )

    monkeypatch.setattr(Reviewer, "review", fake_review)


def test_a_local_review_offers_to_save_the_report(repo: Path, monkeypatch):
    _with_one_finding(monkeypatch)
    _make_interactive(monkeypatch)

    (repo / "app.py").write_text("def f():\n    return 2\n")
    out = repo / "report.md"
    result = runner.invoke(
        app,
        ["review", "--no-llm", "--uncommitted", "-C", str(repo)],
        input=f"s\n{out}\n",
    )

    assert result.exit_code == EXIT_OK, result.output
    assert "Save this review" in flatten(result.output)
    assert "[p] post" not in flatten(result.output)
    assert out.is_file()
    assert "Returns the wrong value" in out.read_text(encoding="utf-8")


def test_declining_the_save_writes_nothing(repo: Path, monkeypatch):
    _with_one_finding(monkeypatch)
    _make_interactive(monkeypatch)
    (repo / "app.py").write_text("def f():\n    return 2\n")

    result = runner.invoke(
        app, ["review", "--no-llm", "--uncommitted", "-C", str(repo)], input="n\n"
    )
    assert result.exit_code == EXIT_OK
    assert "Save this review" in flatten(result.output)
    assert not list(repo.glob("*.md"))


def test_an_empty_path_falls_back_to_the_default_name(repo: Path, monkeypatch):
    from roborak.cli.commands.review import DEFAULT_REPORT_NAME

    _with_one_finding(monkeypatch)
    _make_interactive(monkeypatch)
    (repo / "app.py").write_text("def f():\n    return 2\n")
    monkeypatch.chdir(repo)

    result = runner.invoke(
        app, ["review", "--no-llm", "--uncommitted", "-C", str(repo)], input="s\n\n"
    )
    assert result.exit_code == EXIT_OK, result.output
    assert (repo / DEFAULT_REPORT_NAME).is_file()


def test_a_clean_review_is_not_offered_at_all(repo: Path, monkeypatch):
    """With no findings and no overview there is nothing to save."""
    _make_interactive(monkeypatch)
    (repo / "app.py").write_text("def f():\n    return 2\n")
    result = runner.invoke(
        app, ["review", "--no-llm", "--uncommitted", "-C", str(repo)], input="n\n"
    )
    assert result.exit_code == EXIT_OK
    assert "Save this review" not in flatten(result.output)


def test_no_post_suppresses_the_offer(repo: Path, monkeypatch):
    _with_one_finding(monkeypatch)
    _make_interactive(monkeypatch)
    (repo / "app.py").write_text("def f():\n    return 2\n")
    result = runner.invoke(
        app, ["review", "--no-llm", "--uncommitted", "-C", str(repo), "--no-post"], input="n\n"
    )
    assert result.exit_code == EXIT_OK
    assert "Save this review" not in flatten(result.output)


def test_markdown_already_written_is_not_offered_again(repo: Path, monkeypatch):
    _with_one_finding(monkeypatch)
    _make_interactive(monkeypatch)
    (repo / "app.py").write_text("def f():\n    return 2\n")
    out = repo / "report.md"
    result = runner.invoke(
        app,
        ["review", "--no-llm", "--uncommitted", "-C", str(repo), "--markdown", str(out)],
        input="n\n",
    )
    assert result.exit_code == EXIT_OK
    assert out.is_file()
    assert "Save this review" not in flatten(result.output)


def test_json_mode_is_never_prompted(repo: Path, monkeypatch):
    """A prompt on stdout would break whatever is parsing it."""
    _with_one_finding(monkeypatch)
    _make_interactive(monkeypatch)
    (repo / "app.py").write_text("def f():\n    return 2\n")
    result = runner.invoke(
        app, ["review", "--no-llm", "--uncommitted", "-C", str(repo), "--json"], input="n\n"
    )
    assert result.exit_code == EXIT_OK
    json.loads(result.stdout)


MR_URL = "https://gitlab.com/acme/web/-/merge_requests/298"

MR_DIFF = """\
diff --git a/app/auth.py b/app/auth.py
--- a/app/auth.py
+++ b/app/auth.py
@@ -8,3 +8,4 @@ def get_session(request):
     user_id = request.args.get("user_id")
     if not user_id:
         return None
+    return db.execute("SELECT * FROM s WHERE u = " + user_id)
"""


def test_no_discussions_disables_forge_context_loading(repo: Path, monkeypatch):
    from roborak.core.models import ChangeSet

    observed: list[bool] = []

    class FakeSource:
        include_discussions = True
        max_recovered_file_bytes = 0

        def __init__(self, target, token):
            pass

        def load(self):
            observed.append(self.include_discussions)
            return ChangeSet(origin="gitlab")

    monkeypatch.setattr("roborak.cli.shared.get_token", lambda provider, forge=None: "tok")
    monkeypatch.setattr("roborak.cli.shared.GitLabSource", FakeSource)

    result = runner.invoke(
        app,
        [
            "review",
            "--mr",
            MR_URL,
            "--no-discussions",
            "--no-llm",
            "--no-post",
            "-C",
            str(repo),
        ],
    )

    assert result.exit_code == EXIT_OK
    assert observed == [False]


def _mr_session(monkeypatch):
    """Stand a --mr 298 review up end to end, and report how it was published."""
    from roborak.analysis.reviewer import Reviewer
    from roborak.context.diff import parse_diff
    from roborak.core.models import ChangeSet, Finding, ForgeRef, ReviewResult
    from roborak.core.severity import Category, Severity

    changeset = ChangeSet(
        files=parse_diff(MR_DIFF),
        origin="gitlab",
        head_sha="head333",
        forge_ref=ForgeRef(
            provider="gitlab",
            host="gitlab.com",
            project="acme/web",
            number=298,
            base_sha="base111",
            start_sha="start222",
            head_sha="head333",
        ),
    )

    monkeypatch.setattr("roborak.cli.shared.get_token", lambda provider, forge=None: "tok")
    monkeypatch.setattr(
        "roborak.cli.commands.review.remote_state", lambda target, token: RemoteState()
    )

    class FakeSource:
        def __init__(self, target, token):
            pass

        def load(self):
            return changeset

    monkeypatch.setattr("roborak.cli.shared.GitLabSource", FakeSource)

    def fake_review(self, cs):
        return ReviewResult(
            changeset=cs,
            findings=[
                Finding(
                    file="app/auth.py",
                    start_line=11,
                    end_line=11,
                    severity=Severity.CRITICAL,
                    category=Category.SECURITY,
                    title="SQL injection",
                    body="user_id is concatenated into SQL.",
                )
            ],
        )

    monkeypatch.setattr(Reviewer, "review", fake_review)

    built: dict[str, object] = {}

    class FakePublisher:
        def __init__(
            self,
            *,
            target,
            token,
            post_inline,
            post_summary,
            seen_fingerprints,
            summary_ref=None,
            summary_refreshed=False,
        ):
            built["post_inline"] = post_inline
            built["post_summary"] = post_summary
            built["summary_ref"] = summary_ref
            built["summary_refreshed"] = summary_refreshed

        def publish(self, result):
            from roborak.publish.base import PublishReport

            built["published"] = True
            return PublishReport()

    monkeypatch.setattr("roborak.cli.commands.review.GitLabPublisher", FakePublisher)
    _make_interactive(monkeypatch)
    return built


def test_the_prompt_previews_what_it_would_post(repo: Path, monkeypatch):
    built = _mr_session(monkeypatch)
    result = runner.invoke(
        app, ["review", "--no-llm", "--mr", MR_URL, "-C", str(repo)], input="p\n"
    )

    assert result.exit_code == EXIT_OK, result.output
    assert "Post to gitlab.com acme/web !298?" in unwrapped(result.output)
    assert "the report above, as a comment" in flatten(result.output)
    assert "1 inline comment(s)" in flatten(result.output)
    assert built == {
        "post_inline": True,
        "post_summary": True,
        "published": True,
        "summary_ref": None,
        "summary_refreshed": False,
    }


def test_posting_sends_the_report_and_the_inline_threads(repo: Path, monkeypatch):
    built = _mr_session(monkeypatch)
    result = runner.invoke(
        app, ["review", "--no-llm", "--mr", MR_URL, "-C", str(repo)], input="p\n"
    )

    assert result.exit_code == EXIT_OK, result.output
    assert built["post_inline"] is True
    assert built["post_summary"] is True


def test_the_prompt_can_save_a_forge_review_instead_of_posting(repo: Path, monkeypatch):
    """Both actions are on offer even when there is somewhere to post."""
    built = _mr_session(monkeypatch)
    out = repo / "review.md"
    result = runner.invoke(
        app,
        ["review", "--no-llm", "--mr", MR_URL, "-C", str(repo)],
        input=f"s\n{out}\n",
    )

    assert result.exit_code == EXIT_OK, result.output
    assert "[p] post" in flatten(result.output)
    assert "[s] save as .md" in flatten(result.output)
    assert "published" not in built
    assert out.is_file()
    assert "SQL injection" in out.read_text(encoding="utf-8")


@pytest.mark.parametrize("answer", ["n", "", "whatever"])
def test_anything_that_is_not_a_choice_publishes_nothing(repo: Path, monkeypatch, answer):
    built = _mr_session(monkeypatch)
    result = runner.invoke(
        app, ["review", "--no-llm", "--mr", MR_URL, "-C", str(repo)], input=f"{answer}\n"
    )

    assert result.exit_code == EXIT_OK, result.output
    assert "published" not in built


def test_post_skips_the_prompt_entirely(repo: Path, monkeypatch):
    """An explicit --post is already an answer; asking again would be noise."""
    built = _mr_session(monkeypatch)
    result = runner.invoke(
        app, ["review", "--no-llm", "--mr", MR_URL, "--post", "-C", str(repo)], input="n\n"
    )

    assert result.exit_code == EXIT_OK, result.output
    assert "Post to gitlab.com" not in unwrapped(result.output)
    assert built == {
        "post_inline": True,
        "post_summary": True,
        "published": True,
        "summary_ref": None,
        "summary_refreshed": False,
    }


def _empty_mr_session(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    """Stand a --mr 298 review up over a changeset with no files at all.

    Reports one entry per publisher run, so a test can tell a single clean
    publish from the same review being posted twice.
    """
    from roborak.core.models import ChangeSet, ForgeRef

    changeset = ChangeSet(
        files=[],
        origin="gitlab",
        head_sha="head333",
        forge_ref=ForgeRef(
            provider="gitlab",
            host="gitlab.com",
            project="acme/web",
            number=298,
            base_sha="base111",
            start_sha="start222",
            head_sha="head333",
        ),
    )

    monkeypatch.setattr("roborak.cli.shared.get_token", lambda provider, forge=None: "tok")
    monkeypatch.setattr(
        "roborak.cli.commands.review.remote_state", lambda target, token: RemoteState()
    )

    class FakeSource:
        def __init__(self, target, token):
            pass

        def load(self):
            return changeset

    monkeypatch.setattr("roborak.cli.shared.GitLabSource", FakeSource)

    runs: list[dict[str, object]] = []

    class FakePublisher:
        def __init__(
            self,
            *,
            target,
            token,
            post_inline,
            post_summary,
            seen_fingerprints,
            summary_ref=None,
            summary_refreshed=False,
        ):
            self._run = {"post_inline": post_inline, "post_summary": post_summary}

        def publish(self, result):
            from roborak.publish.base import PublishReport

            runs.append(self._run)
            return PublishReport()

    monkeypatch.setattr("roborak.cli.commands.review.GitLabPublisher", FakePublisher)
    _make_interactive(monkeypatch)
    return runs


def test_post_publishes_the_report_when_there_is_nothing_to_review(repo: Path, monkeypatch):
    """A clean run is still a result: --post says so on the merge request.

    An empty changeset has no inline comments, so the summary is the whole
    comment. Publishing with it switched off would post nothing at all.
    """
    runs = _empty_mr_session(monkeypatch)
    result = runner.invoke(app, ["review", "--no-llm", "--mr", MR_URL, "--post", "-C", str(repo)])

    assert result.exit_code == EXIT_OK, result.output
    assert runs == [{"post_inline": True, "post_summary": True}]


def test_post_publishes_an_empty_changeset_exactly_once(repo: Path, monkeypatch):
    """One review, one publish. A second pass would duplicate the comment."""
    runs = _empty_mr_session(monkeypatch)
    result = runner.invoke(app, ["review", "--no-llm", "--mr", MR_URL, "--post", "-C", str(repo)])

    assert result.exit_code == EXIT_OK, result.output
    assert len(runs) == 1, runs


PR_URL = "https://github.com/acme/web/pull/57"


def _with_github_origin(repo: Path) -> None:
    """Give the repository the remote a bare ``--pr 21`` needs to name a project."""
    subprocess.run(
        ["git", "remote", "add", "origin", "https://github.com/acme/web.git"],
        cwd=repo,
        check=True,
    )


def _empty_pr_session(
    monkeypatch: pytest.MonkeyPatch, *, number: int = 57
) -> list[dict[str, object]]:
    """The same empty review, but stated as --pr and paired with --issue.

    The reported failure came from that combination: an issue alongside a
    stated pull request must not cost the run its target. ``number`` is the
    pull request the forge hands back, so a test can assert the run published
    to the request it was given.
    """
    from roborak.core.models import ChangeSet, ForgeRef
    from roborak.sources.forge import Target

    changeset = ChangeSet(
        files=[],
        origin="github",
        head_sha="head333",
        forge_ref=ForgeRef(
            provider="github",
            host="github.com",
            project="acme/web",
            number=number,
            base_sha="base111",
            head_sha="head333",
        ),
    )

    monkeypatch.setattr("roborak.cli.shared.get_token", lambda provider, forge=None: "tok")
    monkeypatch.setattr("roborak.cli.shared.load_issue", lambda t, tok: _stub_issue())
    monkeypatch.setattr(
        "roborak.cli.commands.review.remote_state", lambda target, token: RemoteState()
    )

    class FakeSource:
        def __init__(self, target: Target, token: str) -> None:
            pass

        def load(self) -> ChangeSet:
            return changeset

    monkeypatch.setattr("roborak.cli.shared.GitHubSource", FakeSource)

    runs: list[dict[str, object]] = []

    class FakePublisher:
        def __init__(
            self,
            *,
            target,
            token,
            post_inline,
            post_summary,
            seen_fingerprints,
            summary_ref=None,
            summary_refreshed=False,
        ):
            self._run = {
                "number": target.number,
                "post_inline": post_inline,
                "post_summary": post_summary,
            }

        def publish(self, result):
            from roborak.publish.base import PublishReport

            runs.append(self._run)
            return PublishReport()

    monkeypatch.setattr("roborak.cli.commands.review.GitHubPublisher", FakePublisher)
    _make_interactive(monkeypatch)
    return runs


def test_post_publishes_an_empty_pull_request_reviewed_against_an_issue(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--pr with --issue still has a target, so an empty review is published."""
    runs = _empty_pr_session(monkeypatch)
    result = runner.invoke(
        app,
        [
            "review",
            "--no-llm",
            "--pr",
            PR_URL,
            "--issue",
            "https://github.com/acme/web/issues/42",
            "--post",
            "-C",
            str(repo),
        ],
    )

    assert result.exit_code == EXIT_OK, result.output
    assert runs == [{"number": 57, "post_inline": True, "post_summary": True}]


def test_post_publishes_an_empty_numbered_pull_request_reviewed_against_an_issue(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reported form: bare numbers, which resolve the project from the remote.

    ``--pr 21`` takes a different road through ``parse_target`` than a full URL
    does -- host and project come from the git remote rather than the argument --
    so the URL test above says nothing about the command that actually failed.
    """
    _with_github_origin(repo)
    runs = _empty_pr_session(monkeypatch, number=21)
    result = runner.invoke(
        app,
        ["review", "--no-llm", "--pr", "21", "--issue", "18", "--post", "-C", str(repo)],
    )

    assert result.exit_code == EXIT_OK, result.output
    assert runs == [{"number": 21, "post_inline": True, "post_summary": True}]


def _gitlab_changeset(files: bool = True):
    """The --mr 298 change, as the forge would hand it over."""
    from roborak.context.diff import parse_diff
    from roborak.core.models import ChangeSet, ForgeRef

    return ChangeSet(
        files=parse_diff(MR_DIFF) if files else [],
        origin="gitlab",
        head_sha="head333",
        forge_ref=ForgeRef(
            provider="gitlab",
            host="gitlab.com",
            project="acme/web",
            number=298,
            base_sha="base111",
            start_sha="start222",
            head_sha="head333",
        ),
    )


def _install_gitlab_session(monkeypatch, published: list, *, files: bool = True) -> None:
    """Wire a --mr run up to a publisher that records each run instead of posting."""
    changeset = _gitlab_changeset(files)
    monkeypatch.setattr("roborak.cli.shared.get_token", lambda provider, forge=None: "tok")

    class FakeSource:
        def __init__(self, target, token):
            pass

        def load(self):
            return changeset

    monkeypatch.setattr("roborak.cli.shared.GitLabSource", FakeSource)

    class FakePublisher:
        def __init__(
            self,
            *,
            target,
            token,
            post_inline,
            post_summary,
            seen_fingerprints,
            summary_ref=None,
            summary_refreshed=False,
        ):
            self._run = {"post_inline": post_inline, "post_summary": post_summary}

        def publish(self, result):
            from roborak.publish.base import PublishReport

            published.append(self._run)
            return PublishReport()

    monkeypatch.setattr("roborak.cli.commands.review.GitLabPublisher", FakePublisher)
    _make_interactive(monkeypatch)


def test_a_clean_rerun_from_another_machine_still_publishes(repo: Path, monkeypatch):
    """#23 end to end: --post finished silently and left the review unrecorded.

    A clean review has no inline comments, so the summary is the only thing it
    has to say. An earlier run had published the overview, but its record of
    that lives in this repo's state directory -- which the machine running now
    does not have. That miss used to switch the summary off and post nothing.
    """
    from roborak.analysis.reviewer import Reviewer
    from roborak.core.models import ReviewResult, Walkthrough
    from roborak.publish.base import RemoteState, SummaryRef

    monkeypatch.setattr(Reviewer, "review", lambda self, cs: ReviewResult(changeset=cs))

    published: list[dict[str, object]] = []
    _install_gitlab_session(monkeypatch, published)

    def already_posted(target, token):
        # The overview rides on the comment, which is all this machine can read.
        return RemoteState(
            summary=SummaryRef(
                edit_path="/notes/9",
                method="PUT",
                flow=_gitlab_changeset().flow_digest,
                walkthrough=Walkthrough(overview="Looks up a session row."),
            )
        )

    monkeypatch.setattr("roborak.cli.commands.review.remote_state", already_posted)
    assert not (repo / ".roborak" / "state.json").exists(), "no local record of the earlier run"

    result = runner.invoke(app, ["review", "--no-llm", "--mr", MR_URL, "--post", "-C", str(repo)])

    assert result.exit_code == EXIT_OK, result.output
    assert published == [{"post_inline": True, "post_summary": True}], (
        "a clean review that publishes nothing is the bug"
    )


def test_no_summary_posts_the_threads_without_the_report(repo: Path, monkeypatch):
    built = _mr_session(monkeypatch)
    result = runner.invoke(
        app,
        ["review", "--no-llm", "--mr", MR_URL, "--no-summary", "-C", str(repo)],
        input="p\n",
    )

    assert result.exit_code == EXIT_OK, result.output
    assert "the report above, as a comment" not in flatten(result.output)
    assert built["post_inline"] is True
    assert built["post_summary"] is False


def test_every_bucket_reaches_the_reader(repo: Path, monkeypatch):
    """Everything is in the one report now, including what cannot go inline."""
    from roborak.analysis.reviewer import Reviewer
    from roborak.context.diff import parse_diff
    from roborak.core.models import ChangeSet, Finding, ForgeRef, ReviewResult
    from roborak.core.severity import Category, Kind, Severity

    _mr_session(monkeypatch)

    changeset = ChangeSet(
        files=parse_diff(MR_DIFF),
        origin="gitlab",
        forge_ref=ForgeRef(
            provider="gitlab",
            host="gitlab.com",
            project="acme/web",
            number=298,
            base_sha="b",
            head_sha="h",
        ),
    )

    def make(line, kind, title):
        return Finding(
            file="app/auth.py",
            start_line=line,
            end_line=line,
            severity=Severity.MAJOR,
            category=Category.SECURITY,
            kind=kind,
            title=title,
            body="Something is wrong here.",
        )

    monkeypatch.setattr(
        Reviewer,
        "review",
        lambda self, cs: ReviewResult(
            changeset=changeset,
            findings=[
                make(11, Kind.POTENTIAL_ISSUE, "Anchorable finding"),
                make(11, Kind.NITPICK, "Small thing"),
                make(900, Kind.POTENTIAL_ISSUE, "Nowhere near the diff"),
            ],
        ),
    )

    result = runner.invoke(
        app, ["review", "--no-llm", "--mr", MR_URL, "-C", str(repo)], input="n\n"
    )
    assert result.exit_code == EXIT_OK, result.output

    report = result.stdout
    assert "**Anchorable finding.**" in report
    assert "**Small thing.**" in report
    assert "**Nowhere near the diff.**" in report

    assert "1 inline comment(s)" in flatten(result.output)


def test_stdout_is_the_report_and_nothing_else(repo: Path, monkeypatch):
    """`roborak review > review.md` has to produce the report, not the report
    with a spinner smeared through it."""
    _with_one_finding(monkeypatch)
    (repo / "app.py").write_text("def f():\n    return 2\n")

    result = runner.invoke(app, ["review", "--no-llm", "--uncommitted", "-C", str(repo)])
    assert result.exit_code == EXIT_OK, result.output

    assert result.stdout.startswith("# ")
    assert "**Returns the wrong value.**" in result.stdout
    assert "<summary>ℹ️ Review info</summary>" in result.stdout, "HTML is passed through verbatim"


def test_the_printed_report_is_exactly_what_the_renderer_produced(repo: Path, monkeypatch):
    """No rich wrapping, no markup interpretation: byte for byte the document."""
    from roborak.analysis.reviewer import Reviewer
    from roborak.render import markdown as markdown_render

    _with_one_finding(monkeypatch)
    (repo / "app.py").write_text("def f():\n    return 2\n")

    captured = {}
    original = Reviewer.review

    def spy(self, changeset):
        captured["result"] = original(self, changeset)
        return captured["result"]

    monkeypatch.setattr(Reviewer, "review", spy)

    result = runner.invoke(app, ["review", "--no-llm", "--uncommitted", "-C", str(repo)])
    assert result.exit_code == EXIT_OK, result.output
    assert result.stdout == markdown_render.render(captured["result"]) + "\n"


def test_a_reader_gets_it_rendered(repo: Path, monkeypatch):
    """Under CliRunner stdout is not a tty, so force the branch a person takes."""
    _with_one_finding(monkeypatch)
    monkeypatch.setattr("roborak.cli.shared.stdout_is_a_terminal", lambda: True)
    (repo / "app.py").write_text("def f():\n    return 2\n")

    result = runner.invoke(app, ["review", "--no-llm", "--uncommitted", "-C", str(repo)])
    assert result.exit_code == EXIT_OK, result.output

    out = result.stdout
    assert "<details>" not in out, "the HTML is rendered away, not printed"
    assert "<!-- roborak:v1" not in out
    assert "Actionable comments (1)" in out
    assert "Returns the wrong value." in out
    assert "app.py:2" in out, "a path the reader can open"
    assert "🤖 Prompt" not in out, "the agent prompt is written for a machine"
    assert "Review info" not in out


def test_full_restores_what_the_reader_does_not_normally_want(repo: Path, monkeypatch):
    _with_one_finding(monkeypatch)
    monkeypatch.setattr("roborak.cli.shared.stdout_is_a_terminal", lambda: True)
    (repo / "app.py").write_text("def f():\n    return 2\n")

    result = runner.invoke(app, ["review", "--no-llm", "--uncommitted", "-C", str(repo), "--full"])
    assert result.exit_code == EXIT_OK, result.output
    assert "🤖 Prompt for AI Agents" in result.stdout
    assert "Review info" in result.stdout


def test_a_pipe_still_gets_the_markdown(repo: Path, monkeypatch):
    """`roborak review > review.md` must give back the publishable document."""
    _with_one_finding(monkeypatch)
    monkeypatch.setattr("roborak.cli.shared.stdout_is_a_terminal", lambda: False)
    (repo / "app.py").write_text("def f():\n    return 2\n")

    result = runner.invoke(app, ["review", "--no-llm", "--uncommitted", "-C", str(repo)])
    assert result.exit_code == EXIT_OK, result.output
    assert "<details>" in result.stdout
    assert "<summary>ℹ️ Review info</summary>" in result.stdout


def test_panels_restores_the_rich_view(repo: Path, monkeypatch):
    _with_one_finding(monkeypatch)
    (repo / "app.py").write_text("def f():\n    return 2\n")

    result = runner.invoke(
        app, ["review", "--no-llm", "--uncommitted", "-C", str(repo), "--panels"]
    )
    assert result.exit_code == EXIT_OK, result.output
    assert "<summary>" not in flatten(result.output), "the panels are not the report"
    assert "Returns the wrong value" in flatten(result.output)
    assert "🎯 Functional Correctness" in flatten(result.output)


# --- deciding whether the overview has to be written again ----------------------


def _overview_session(repo: Path):
    """The three things the overview decision reads off a session."""
    from types import SimpleNamespace

    from roborak.context.diff import parse_diff
    from roborak.core.models import ChangeSet
    from roborak.sources.forge import Target

    changeset = ChangeSet(files=parse_diff(MR_DIFF), origin="gitlab", head_sha="head333")
    return SimpleNamespace(
        changeset=changeset,
        repo=repo,
        target=Target("gitlab", "gitlab.com", "acme/web", 298),
    )


def _plan(session, remote, **kwargs):
    from roborak.cli.commands.review import _overview_plan

    options = {"no_summary": False, "repost": False, "publishing": True, **kwargs}
    return _overview_plan(session, remote, **options)


def test_a_first_post_writes_the_overview(repo: Path):
    from roborak.publish.base import RemoteState

    plan = _plan(_overview_session(repo), RemoteState())
    assert plan.generate and plan.post_summary and plan.ref is None


def test_an_unmoved_change_does_not_pay_for_a_second_overview(repo: Path):
    """The comment carries the overview, so an unmoved change reuses it for free."""
    from roborak.core.models import Walkthrough
    from roborak.publish.base import RemoteState, SummaryRef

    session = _overview_session(repo)
    remote = RemoteState(
        summary=SummaryRef(
            edit_path="/notes/9",
            method="PUT",
            flow=session.changeset.flow_digest,
            walkthrough=Walkthrough(overview="Looks up a session row."),
        )
    )
    plan = _plan(session, remote)

    assert not plan.generate, "the published overview still holds; do not pay for it again"
    assert plan.post_summary, "the verdict is this run's, even when the overview is not"
    assert plan.cached is not None and plan.cached.overview == "Looks up a session row."


def test_an_unmoved_change_publishes_the_verdict_from_a_machine_that_never_posted(repo: Path):
    """The regression behind #23: a local cache miss silenced --post entirely.

    ``.roborak/state.json`` never leaves the machine that wrote it, so CI and
    every other checkout missed it. Switching the summary off there left a clean
    review with no inline comments to post and nothing else to say.
    """
    from roborak.publish.base import RemoteState, SummaryRef

    session = _overview_session(repo)
    remote = RemoteState(
        summary=SummaryRef(edit_path="/notes/9", method="PUT", flow=session.changeset.flow_digest)
    )
    plan = _plan(session, remote)

    assert plan.post_summary, "a run that publishes nothing at all is the bug"
    assert plan.generate, "nothing to reuse, so re-narrate rather than edit the overview away"
    assert plan.ref is not None, "still an edit, never a second comment"


def test_an_unmoved_change_reuses_the_overview_this_machine_still_has(repo: Path):
    from roborak.core.models import Walkthrough
    from roborak.publish.base import RemoteState, SummaryRef
    from roborak.state.store import StateStore, review_key

    session = _overview_session(repo)
    key = review_key("gitlab", "gitlab.com", "acme/web", 298)
    StateStore(repo).record(
        key,
        [],
        "head333",
        flow_digest=session.changeset.flow_digest,
        walkthrough=Walkthrough(overview="Looks up a session row.").model_dump(),
    )

    remote = RemoteState(
        summary=SummaryRef(edit_path="/notes/9", method="PUT", flow=session.changeset.flow_digest)
    )
    plan = _plan(session, remote)

    assert not plan.generate
    assert plan.post_summary
    assert plan.cached is not None
    assert plan.cached.overview == "Looks up a session row."


def test_a_moved_change_is_narrated_again_over_the_old_comment(repo: Path):
    from roborak.publish.base import RemoteState, SummaryRef

    remote = RemoteState(
        summary=SummaryRef(edit_path="/notes/9", method="PUT", flow="0" * 16),
    )
    plan = _plan(_overview_session(repo), remote)

    assert plan.generate and plan.refreshed
    assert plan.ref is not None and plan.post_summary


def test_an_overview_posted_before_the_marker_existed_is_refreshed_once(repo: Path):
    from roborak.publish.base import RemoteState, SummaryRef

    remote = RemoteState(summary=SummaryRef(edit_path="/notes/9", method="PUT", flow=""))
    plan = _plan(_overview_session(repo), remote)

    assert plan.generate and plan.refreshed and plan.ref is not None


def test_repost_forces_a_fresh_overview(repo: Path):
    from roborak.publish.base import RemoteState, SummaryRef

    session = _overview_session(repo)
    remote = RemoteState(
        summary=SummaryRef(edit_path="/notes/9", method="PUT", flow=session.changeset.flow_digest)
    )
    plan = _plan(session, remote, repost=True)

    assert plan.generate and plan.post_summary
    assert plan.ref is None  # a fresh overview goes up beside the old one


def test_no_summary_leaves_the_overview_pass_exactly_as_it_was(repo: Path):
    from roborak.publish.base import RemoteState, SummaryRef

    session = _overview_session(repo)
    remote = RemoteState(
        summary=SummaryRef(edit_path="/notes/9", method="PUT", flow=session.changeset.flow_digest)
    )
    plan = _plan(session, remote, no_summary=True)

    assert plan.generate and not plan.post_summary


def test_a_local_review_has_no_published_overview_to_reuse(repo: Path):
    plan = _plan(_overview_session(repo), None, publishing=False)
    assert plan.generate and not plan.post_summary
