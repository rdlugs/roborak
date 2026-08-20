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

    def capture(model: str, llm=None) -> str | None:
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
    # The config file is the other way out, so the error has to mention it.
    assert "forge.tokens.gitlab" in result.output


def test_config_show_reports_a_configured_forge_host(repo: Path):
    (repo / ".roborak.yaml").write_text("forge:\n  hosts:\n    gitlab: gitlab.acme.com\n")
    result = runner.invoke(app, ["config", "show", "-C", str(repo)])
    assert result.exit_code == EXIT_OK
    assert "gitlab.acme.com" in result.output


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
    assert "Could not tell which forge" in unconfigured.output

    (repo / ".roborak.yaml").write_text("forge:\n  hosts:\n    gitlab: git.corp.example\n")
    result = runner.invoke(app, ["review", "--no-llm", "-C", str(repo), "--issue", "24"])
    # The domain is now recognisable, so the run gets as far as needing a token.
    assert result.exit_code == EXIT_ERROR
    assert "needs a gitlab token" in result.output


def test_config_show_redacts_a_forge_token(repo: Path):
    (repo / ".roborak.yaml").write_text("forge:\n  tokens:\n    gitlab: glpat-secret\n")
    result = runner.invoke(app, ["config", "show", "-C", str(repo)])
    assert result.exit_code == EXIT_OK
    assert "glpat-secret" not in result.output
    assert "gitlab" in result.output


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


# -- --issue ---------------------------------------------------------------


@pytest.mark.parametrize("command", ["review", "describe", "improve", "ask"])
def test_issue_flag_is_offered_by_every_command(command):
    result = runner.invoke(app, [command, "--help"])
    assert result.exit_code == EXIT_OK
    assert "--issue" in result.output


def test_issue_without_a_recognisable_forge_fails_clearly(repo: Path, monkeypatch):
    # A bare number and a remote that names neither forge: guessing would be worse
    # than asking.
    monkeypatch.setattr("roborak.cli.shared.detect_provider", lambda *a, **k: None)
    result = runner.invoke(app, ["review", "--no-llm", "--issue", "42", "-C", str(repo)])
    assert result.exit_code == EXIT_ERROR
    assert "full issue URL" in result.output


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
    assert "GITHUB_TOKEN" in result.output


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
    assert "pull request #57" in result.output


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
    assert "No findings" in result.output
    assert "against #42" in result.output


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
    assert "no linked change" in result.output


# -- config init --global --------------------------------------------------


def unwrapped(output: str) -> str:
    """Rich hard-wraps at the terminal width, splitting long paths mid-token."""
    return output.replace("\n", "")


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
    # The parent directory will not exist on a fresh machine.
    assert user_config.parent.is_dir()


def test_global_init_is_not_world_readable(user_config: Path):
    # It is where the README tells people to put API keys.
    runner.invoke(app, ["config", "init", "--global"])
    assert user_config.stat().st_mode & 0o077 == 0


def test_global_init_refuses_to_overwrite_without_force(user_config: Path):
    assert runner.invoke(app, ["config", "init", "--global"]).exit_code == EXIT_OK
    user_config.write_text("version: 1\n# hand-edited\n")

    second = runner.invoke(app, ["config", "init", "--global"])
    assert second.exit_code == EXIT_ERROR
    assert "# hand-edited" in user_config.read_text(), "must not clobber a real config"

    assert runner.invoke(app, ["config", "init", "--global", "--force"]).exit_code == EXIT_OK
    assert "# hand-edited" not in user_config.read_text()


def test_global_and_dir_together_are_refused(repo: Path, user_config: Path):
    result = runner.invoke(app, ["config", "init", "--global", "-C", str(repo)])
    assert result.exit_code == EXIT_ERROR
    assert not user_config.exists()
    assert not (repo / ".roborak.yaml").exists()


def test_global_init_is_picked_up_by_show(repo: Path, user_config: Path):
    # `repo` also redirects USER_CONFIG_PATH, so it must be set up first.
    runner.invoke(app, ["config", "init", "--global"])
    shown = runner.invoke(app, ["config", "show", "-C", str(repo)])
    assert shown.exit_code == EXIT_OK
    assert str(user_config) in unwrapped(shown.output)


def test_the_scaffolded_file_is_the_commented_template(repo: Path):
    runner.invoke(app, ["config", "init", "-C", str(repo)])
    written = (repo / ".roborak.yaml").read_text()

    # A bare model dump would carry none of this.
    assert written.startswith("# roborak configuration")
    assert "check_requirements" in written
    assert "# null autodetects whatever is on PATH." in written


def test_the_template_ships_inside_the_package():
    from importlib import resources

    from roborak.cli.commands.config_cmd import TEMPLATE_NAME, template_text

    # Resolving it by walking up from __file__ breaks in an installed wheel.
    assert (resources.files("roborak") / TEMPLATE_NAME).is_file()
    assert template_text().startswith("# roborak configuration")


def test_config_show_names_an_explicit_config_file(repo: Path, tmp_path: Path):
    explicit = tmp_path / "elsewhere.yaml"
    explicit.write_text("version: 1\n")
    shown = runner.invoke(app, ["config", "show", "-C", str(repo), "--config", str(explicit)])
    assert shown.exit_code == EXIT_OK
    assert str(explicit) in unwrapped(shown.output)


# -- the end-of-review prompt ----------------------------------------------


def test_the_new_flags_are_documented():
    result = runner.invoke(app, ["review", "--help"])
    assert result.exit_code == EXIT_OK
    for flag in ("--no-post", "--no-walkthrough"):
        assert flag in result.output


def test_a_non_tty_is_never_prompted(repo: Path):
    """CliRunner and every CI runner fail the tty test, so the run must not block."""
    (repo / "app.py").write_text("def f():\n    return 2\n")
    result = runner.invoke(app, ["review", "--no-llm", "--uncommitted", "-C", str(repo)])
    assert result.exit_code == EXIT_OK
    assert "Save this review" not in result.output


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
    assert "Save this review" in result.output
    # A local diff has nowhere to post, so posting is never offered.
    assert "[p] post" not in result.output
    assert out.is_file()
    assert "Returns the wrong value" in out.read_text()


def test_declining_the_save_writes_nothing(repo: Path, monkeypatch):
    _with_one_finding(monkeypatch)
    _make_interactive(monkeypatch)
    (repo / "app.py").write_text("def f():\n    return 2\n")

    result = runner.invoke(
        app, ["review", "--no-llm", "--uncommitted", "-C", str(repo)], input="n\n"
    )
    assert result.exit_code == EXIT_OK
    assert "Save this review" in result.output
    assert not list(repo.glob("*.md"))


def test_an_empty_path_falls_back_to_the_default_name(repo: Path, monkeypatch):
    from roborak.cli.commands.review import DEFAULT_REPORT_NAME

    _with_one_finding(monkeypatch)
    _make_interactive(monkeypatch)
    (repo / "app.py").write_text("def f():\n    return 2\n")
    monkeypatch.chdir(repo)  # the default name is relative, so do not litter the project

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
    assert "Save this review" not in result.output


def test_no_post_suppresses_the_offer(repo: Path, monkeypatch):
    _with_one_finding(monkeypatch)
    _make_interactive(monkeypatch)
    (repo / "app.py").write_text("def f():\n    return 2\n")
    result = runner.invoke(
        app, ["review", "--no-llm", "--uncommitted", "-C", str(repo), "--no-post"], input="n\n"
    )
    assert result.exit_code == EXIT_OK
    assert "Save this review" not in result.output


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
    assert "Save this review" not in result.output


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


# -- the publish prompt on a merge request ---------------------------------


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
        def __init__(self, *, target, token, post_inline, post_summary, seen_fingerprints):
            built["post_inline"] = post_inline
            built["post_summary"] = post_summary

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
    assert "the report above, as a comment" in result.output
    assert "1 inline comment(s)" in result.output
    assert built == {"post_inline": True, "post_summary": True, "published": True}


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
    assert "[p] post" in result.output
    assert "[s] save as .md" in result.output
    assert "published" not in built
    assert out.is_file()
    assert "SQL injection" in out.read_text()


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
    assert built == {"post_inline": True, "post_summary": True, "published": True}


def test_no_summary_posts_the_threads_without_the_report(repo: Path, monkeypatch):
    built = _mr_session(monkeypatch)
    result = runner.invoke(
        app,
        ["review", "--no-llm", "--mr", MR_URL, "--no-summary", "-C", str(repo)],
        input="p\n",
    )

    assert result.exit_code == EXIT_OK, result.output
    assert "the report above, as a comment" not in result.output
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

    # Only the anchorable one would also become an inline thread.
    assert "1 inline comment(s)" in result.output


# -- the report is the output ----------------------------------------------


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
    assert "<summary>" not in result.output, "the panels are not the report"
    assert "Returns the wrong value" in result.output
    assert "🎯 Functional Correctness" in result.output
