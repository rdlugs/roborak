"""Static-analysis adapters and runner.

Adapter parsing is tested against output captured from the real tools, so a
format change breaks a test rather than silently producing zero findings. The
runner is additionally exercised against a real ruff install.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from roborak.context.diff import parse_diff
from roborak.core.config import StaticConfig
from roborak.core.models import ChangeSet
from roborak.core.severity import Category, Severity
from roborak.static.adapters.eslint import EslintAdapter
from roborak.static.adapters.mypy import MypyAdapter
from roborak.static.adapters.phpstan import PhpstanAdapter
from roborak.static.adapters.ruff import RuffAdapter
from roborak.static.adapters.semgrep import SemgrepAdapter
from roborak.static.normalize import classify_ruff, classify_semgrep
from roborak.static.runner import StaticRunner, _safe_environment

RUFF_OUTPUT = json.dumps(
    [
        {
            "code": "S602",
            "name": "subprocess-popen-with-shell-equals-true",
            "message": "`subprocess` call with `shell=True` identified, security issue",
            "filename": "/repo/svc.py",
            "location": {"row": 9, "column": 5},
            "end_location": {"row": 9, "column": 60},
            "url": "https://docs.astral.sh/ruff/rules/subprocess-popen-with-shell-equals-true",
            "fix": None,
        },
        {
            "code": "F821",
            "name": "undefined-name",
            "message": "Undefined name `undefined_variable`",
            "filename": "/repo/svc.py",
            "location": {"row": 16, "column": 12},
            "end_location": {"row": 16, "column": 30},
            "url": None,
            "fix": None,
        },
    ]
)

MYPY_OUTPUT = (
    '{"file": "/repo/svc.py", "line": 5, "column": 0, "end_line": 5, "end_column": 12, '
    '"message": "Function is missing a type annotation", "hint": null, '
    '"code": "no-untyped-def", "severity": "error"}\n'
    '{"file": "/repo/svc.py", "line": 9, "column": 4, "end_line": 9, "end_column": 8, '
    '"message": "Returning Any from function declared to return \\"int\\"", "hint": null, '
    '"code": "no-any-return", "severity": "error"}\n'
    "/repo/svc.py:5: note: some plain-text note mypy also prints\n"
)

SEMGREP_OUTPUT = json.dumps(
    {
        "results": [
            {
                "check_id": "python.lang.security.audit.dangerous-subprocess-use",
                "path": "svc.py",
                "start": {"line": 9, "col": 5},
                "end": {"line": 9, "col": 60},
                "extra": {"severity": "WARNING", "message": "Detected subprocess with shell=True."},
            }
        ],
        "errors": [],
    }
)

ESLINT_OUTPUT = json.dumps(
    [
        {
            "filePath": "/repo/app.js",
            "messages": [
                {
                    "ruleId": "no-eval",
                    "severity": 2,
                    "message": "eval can be harmful.",
                    "line": 12,
                    "endLine": 12,
                },
                {
                    "ruleId": "no-unused-vars",
                    "severity": 1,
                    "message": "'x' is assigned a value but never used.",
                    "line": 3,
                    "endLine": 3,
                },
                {"ruleId": None, "severity": 2, "message": "Parsing error", "line": None},
            ],
        }
    ]
)

PHPSTAN_OUTPUT = json.dumps(
    {
        "totals": {"errors": 0, "file_errors": 1},
        "files": {
            "app/Http/Controllers/UserController.php": {
                "errors": 1,
                "messages": [
                    {
                        "message": "Call to an undefined method App\\Models\\User::findByEmail().",
                        "line": 42,
                        "ignorable": True,
                        "identifier": "method.notFound",
                    },
                    {"message": "File-level message", "line": None, "ignorable": True},
                ],
            }
        },
    }
)


def test_ruff_parsing():
    findings = RuffAdapter().parse(RUFF_OUTPUT, "", 1)
    assert len(findings) == 2

    security = next(f for f in findings if f.rule_id == "ruff/S602")
    assert security.category is Category.SECURITY
    assert security.severity is Severity.MAJOR
    assert security.start_line == 9
    assert security.tool == "ruff"
    assert security.source == "static"
    assert "docs.astral.sh" in security.body

    undefined = next(f for f in findings if f.rule_id == "ruff/F821")
    assert undefined.severity is Severity.CRITICAL
    assert undefined.category is Category.BUG


def test_mypy_parsing_skips_plain_text_notes():
    findings = MypyAdapter().parse(MYPY_OUTPUT, "", 1)
    assert len(findings) == 2
    assert all(f.category is Category.BUG for f in findings)
    assert findings[0].rule_id == "mypy/no-untyped-def"
    assert findings[0].start_line == 5


def test_semgrep_parsing_treats_audit_rules_as_security():
    findings = SemgrepAdapter().parse(SEMGREP_OUTPUT, "", 1)
    assert len(findings) == 1
    assert findings[0].category is Category.SECURITY
    assert findings[0].severity is Severity.MAJOR
    assert findings[0].file == "svc.py"


def test_eslint_parsing_drops_findings_with_no_location():
    findings = EslintAdapter().parse(ESLINT_OUTPUT, "", 1)
    assert len(findings) == 2
    assert {f.rule_id for f in findings} == {"eslint/no-eval", "eslint/no-unused-vars"}
    assert next(f for f in findings if f.rule_id == "eslint/no-eval").category is Category.SECURITY


def test_phpstan_parsing_drops_file_level_messages():
    findings = PhpstanAdapter().parse(PHPSTAN_OUTPUT, "", 1)
    assert len(findings) == 1
    assert findings[0].start_line == 42
    assert findings[0].rule_id == "phpstan/method.notFound"


@pytest.mark.parametrize(
    "adapter",
    [RuffAdapter(), MypyAdapter(), SemgrepAdapter(), EslintAdapter(), PhpstanAdapter()],
)
@pytest.mark.parametrize("junk", ["", "not json at all", "null", "[]", "{}"])
def test_adapters_never_raise_on_junk(adapter, junk):
    assert adapter.parse(junk, "", 1) == []


def test_phpstan_tolerates_progress_output_before_json():
    assert (
        len(PhpstanAdapter().parse("Note: Using configuration file\n" + PHPSTAN_OUTPUT, "", 1)) == 1
    )


@pytest.mark.parametrize(
    ("code", "category", "severity"),
    [
        ("S608", Category.SECURITY, Severity.MAJOR),
        ("F821", Category.BUG, Severity.CRITICAL),
        ("F401", Category.BUG, Severity.MINOR),
        ("E501", Category.STYLE, Severity.INFO),
        ("PERF401", Category.PERFORMANCE, Severity.MINOR),
        ("B008", Category.BUG, Severity.MAJOR),
        ("ZZZ999", Category.MAINTAINABILITY, Severity.MINOR),
    ],
)
def test_ruff_classification(code, category, severity):
    assert classify_ruff(code) == (category, severity)


def test_longest_prefix_wins():
    """`PERF` must beat `P`-less generic matching, and `F82` must beat `F`."""
    assert classify_ruff("F821")[1] is Severity.CRITICAL
    assert classify_ruff("F841")[1] is Severity.MINOR
    assert classify_ruff("PERF203")[0] is Category.PERFORMANCE


def test_semgrep_non_security_rules_keep_their_severity():
    assert classify_semgrep("WARNING", "python.lang.best-practice.foo")[1] is Severity.MINOR
    assert classify_semgrep("ERROR", "python.lang.correctness.bar") == (
        Category.BUG,
        Severity.MAJOR,
    )


@pytest.fixture
def repo(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.delenv("CI", raising=False)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
    for key, value in (("user.email", "t@e.com"), ("user.name", "T")):
        subprocess.run(["git", "config", key, value], cwd=tmp_path, check=True)
    (tmp_path / "pyproject.toml").write_text('[tool.ruff.lint]\nselect = ["E", "F", "B", "S"]\n')
    return tmp_path


def changeset_for(repo: Path, path: str) -> ChangeSet:
    diff = subprocess.run(
        ["git", "diff", "HEAD", "--", path] if _has_commit(repo) else ["git", "diff"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return ChangeSet(files=parse_diff(diff))


def _has_commit(repo: Path) -> bool:
    return (
        subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD"], cwd=repo, capture_output=True
        ).returncode
        == 0
    )


def test_disabled_runner_does_nothing(repo: Path):
    runner = StaticRunner(repo=repo, config=StaticConfig(enabled=False))
    assert runner.run(ChangeSet()) == []


def test_static_subprocess_environment_drops_credentials(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "secret")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
    monkeypatch.setenv("PATH", "/bin")
    env = _safe_environment()
    assert env["PATH"] == "/bin"
    assert "GITHUB_TOKEN" not in env
    assert "AWS_SECRET_ACCESS_KEY" not in env


def test_auto_static_analysis_refuses_unsandboxed_ci(repo: Path, monkeypatch, caplog):
    monkeypatch.setenv("CI", "true")
    monkeypatch.setattr("roborak.static.runner.shutil.which", lambda name: None)
    assert StaticRunner(repo=repo, config=StaticConfig()).run(ChangeSet()) == []
    assert "bubblewrap is unavailable" in caplog.text


def test_tool_selection_filters_adapters(repo: Path):
    runner = StaticRunner(repo=repo, config=StaticConfig(tools=["ruff"]))
    assert [a.name for a in runner._selected_adapters()] == ["ruff"]


def test_unknown_tool_name_selects_nothing(repo: Path):
    runner = StaticRunner(repo=repo, config=StaticConfig(tools=["nonexistent"]))
    assert runner._selected_adapters() == []


@pytest.mark.skipif(shutil.which("ruff") is None, reason="ruff not installed")
def test_runner_finds_real_problems_on_changed_lines(repo: Path):
    (repo / "svc.py").write_text("def ok():\n    return 1\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)

    (repo / "svc.py").write_text(
        "import subprocess\n\n\ndef ok():\n    return 1\n\n\n"
        "def bad(target):\n    subprocess.call('rm -rf ' + target, shell=True)\n"
    )
    changeset = changeset_for(repo, "svc.py")
    findings = StaticRunner(repo=repo, config=StaticConfig()).run(changeset)

    codes = {f.rule_id for f in findings}
    assert "ruff/S602" in codes, f"expected the shell=True finding, got {codes}"
    assert all(f.file == "svc.py" for f in findings), "paths must be repo-relative"


@pytest.mark.skipif(shutil.which("ruff") is None, reason="ruff not installed")
def test_runner_ignores_problems_the_change_did_not_touch(repo: Path):
    """The single most important noise control: do not report pre-existing issues."""
    (repo / "legacy.py").write_text(
        "import subprocess\n\n\ndef old_and_bad(t):\n"
        "    subprocess.call('rm -rf ' + t, shell=True)\n"
    )
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)

    with (repo / "legacy.py").open("a") as handle:
        handle.write("\n\ndef newly_added():\n    return 2\n")

    changeset = changeset_for(repo, "legacy.py")
    findings = StaticRunner(repo=repo, config=StaticConfig()).run(changeset)
    assert findings == [], f"pre-existing issues must not be reported, got {findings}"


@pytest.mark.skipif(shutil.which("ruff") is None, reason="ruff not installed")
def test_runner_respects_the_projects_own_ruff_config(repo: Path):
    """A rule the project switched off must stay off."""
    (repo / "pyproject.toml").write_text('[tool.ruff.lint]\nselect = ["F"]\n')
    (repo / "svc.py").write_text("x = 1\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)

    (repo / "svc.py").write_text(
        "import subprocess\n\n\ndef bad(t):\n    subprocess.call(t, shell=True)\n"
    )
    changeset = changeset_for(repo, "svc.py")
    findings = StaticRunner(repo=repo, config=StaticConfig()).run(changeset)
    assert not any((f.rule_id or "").startswith("ruff/S") for f in findings)


def test_missing_files_are_skipped_not_fatal(repo: Path):
    """A path in the diff that is gone from disk must not abort the whole run."""
    changeset = ChangeSet(
        files=parse_diff(
            "diff --git a/ghost.py b/ghost.py\n--- a/ghost.py\n+++ b/ghost.py\n"
            "@@ -1,1 +1,2 @@\n x = 1\n+y = 2\n"
        )
    )
    assert StaticRunner(repo=repo, config=StaticConfig()).run(changeset) == []


def test_timeout_is_survivable(repo: Path, monkeypatch):
    (repo / "svc.py").write_text("x = 1\n")

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="ruff", timeout=1)

    monkeypatch.setattr(subprocess, "run", timeout)
    changeset = ChangeSet(
        files=parse_diff(
            "diff --git a/svc.py b/svc.py\n--- a/svc.py\n+++ b/svc.py\n"
            "@@ -1,1 +1,2 @@\n x = 1\n+y = 2\n"
        )
    )
    assert StaticRunner(repo=repo, config=StaticConfig()).run(changeset) == []


def test_semgrep_needs_a_project_config(repo: Path):
    from roborak.core.models import ChangedFile

    files = [ChangedFile(path="a.py", language="python")]
    adapter = SemgrepAdapter()
    assert adapter.config_path(repo) is None
    assert not adapter.is_available(repo, files)


def test_local_binary_is_preferred_over_a_global_one(repo: Path):
    local = repo / "node_modules" / ".bin"
    local.mkdir(parents=True)
    binary = local / "eslint"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    assert EslintAdapter().find_binary(repo) == str(binary)
