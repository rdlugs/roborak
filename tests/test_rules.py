"""Custom rules: loading, scoping, and reaching the prompt."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from roborak.core.models import ChangedFile, ChangeSet
from roborak.core.severity import Category, Severity
from roborak.rules.loader import EXAMPLE_RULE, RuleError, load_rules, load_rules_at_ref, parse_rule
from roborak.rules.matcher import applies_to, matching_rules, rules_for_prompt

FULL_RULE = textwrap.dedent(
    """\
    ---
    id: no-raw-sql
    paths: ["app/**/*.php", "**/*.py"]
    severity: critical
    category: security
    ---
    Never build SQL by string concatenation. Use bound parameters.
    """
)


def write(directory: Path, name: str, content: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(content, encoding="utf-8")
    return path


def test_parses_frontmatter(tmp_path: Path):
    rule = parse_rule(write(tmp_path, "r.md", FULL_RULE))
    assert rule.id == "no-raw-sql"
    assert rule.severity is Severity.CRITICAL
    assert rule.category is Category.SECURITY
    assert rule.paths == ["app/**/*.php", "**/*.py"]
    assert rule.body.startswith("Never build SQL")
    assert rule.qualified_id == "roborak/no-raw-sql"


def test_a_file_without_frontmatter_is_still_a_rule(tmp_path: Path):
    """The lowest-friction way to add a standard: write a sentence in a file."""
    rule = parse_rule(write(tmp_path, "keep-it-simple.md", "Prefer early returns."))
    assert rule.id == "keep-it-simple"
    assert rule.body == "Prefer early returns."
    assert rule.paths == []
    assert rule.severity is Severity.MAJOR


def test_id_defaults_to_the_filename(tmp_path: Path):
    rule = parse_rule(write(tmp_path, "my-rule.md", "---\nseverity: minor\n---\nDo the thing."))
    assert rule.id == "my-rule"
    assert rule.severity is Severity.MINOR


@pytest.mark.parametrize(
    ("content", "fragment"),
    [
        ("", "empty"),
        ("---\nid: x\n---\n\n", "no text"),
        ("---\n- not a mapping\n---\nBody.", "must be a mapping"),
        ("---\nid: x\nseverity: catastrophic\n---\nBody.", "severity"),
        ("---\nid: [unclosed\n---\nBody.", "invalid frontmatter"),
    ],
)
def test_broken_rules_are_rejected_with_a_reason(tmp_path: Path, content, fragment):
    path = write(tmp_path, "bad.md", content)
    with pytest.raises(RuleError, match=fragment):
        parse_rule(path)


def test_a_broken_rule_does_not_disable_the_others(tmp_path: Path, caplog):
    rules_dir = tmp_path / ".roborak" / "rules"
    write(rules_dir, "good.md", FULL_RULE)
    write(rules_dir, "broken.md", "---\n- not a mapping\n---\nBody.")

    rules = load_rules(tmp_path, ".roborak/rules")
    assert [r.id for r in rules] == ["no-raw-sql"]


def test_disabled_rules_are_left_out(tmp_path: Path):
    rules_dir = tmp_path / ".roborak" / "rules"
    write(rules_dir, "off.md", "---\nid: off\nenabled: false\n---\nIgnore me.")
    write(rules_dir, "on.md", "---\nid: on\n---\nApply me.")
    assert [r.id for r in load_rules(tmp_path, ".roborak/rules")] == ["on"]


def test_rules_are_found_recursively(tmp_path: Path):
    write(tmp_path / ".roborak" / "rules" / "security", "a.md", "Rule A.")
    write(tmp_path / ".roborak" / "rules", "b.md", "Rule B.")
    assert len(load_rules(tmp_path, ".roborak/rules")) == 2


def test_missing_rules_directory_is_fine(tmp_path: Path):
    assert load_rules(tmp_path, ".roborak/rules") == []


def test_rules_can_be_loaded_from_the_trusted_base_revision(tmp_path: Path):
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=tmp_path, check=True)
    path = write(tmp_path / ".roborak" / "rules", "policy.md", "Trusted base rule.")
    write(tmp_path / ".roborak" / "rules", "broken.md", "")
    subprocess.run(["git", "add", ".roborak/rules"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=tmp_path, check=True)
    path.write_text("Changed untrusted rule.")
    rules = load_rules_at_ref(tmp_path, ".roborak/rules", "HEAD")
    assert rules is not None and rules[0].body == "Trusted base rule."
    assert load_rules_at_ref(tmp_path, ".roborak/rules", "missing-ref") is None


def test_the_shipped_example_rule_is_valid(tmp_path: Path):
    rule = parse_rule(write(tmp_path, "no-raw-sql.md", EXAMPLE_RULE))
    assert rule.id == "no-raw-sql"
    assert rule.severity is Severity.MAJOR


# -- scoping ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("app/Models/User.php", True),
        ("src/main.py", True),  # matched by **/*.py
        ("main.py", True),  # top-level, which plain fnmatch would miss
        ("src/main.go", False),
        ("config/app.php", False),  # outside app/
    ],
)
def test_path_scoping(tmp_path: Path, path, expected):
    rule = parse_rule(write(tmp_path, "r.md", FULL_RULE))
    assert applies_to(rule, ChangedFile(path=path)) is expected


def test_a_rule_without_paths_applies_everywhere(tmp_path: Path):
    rule = parse_rule(write(tmp_path, "r.md", "Anything goes."))
    assert applies_to(rule, ChangedFile(path="whatever/thing.rs"))


def test_language_scoping(tmp_path: Path):
    rule = parse_rule(write(tmp_path, "r.md", "---\nid: x\nlanguages: [python]\n---\nPython only."))
    assert applies_to(rule, ChangedFile(path="a.py", language="python"))
    assert not applies_to(rule, ChangedFile(path="a.go", language="go"))


def test_only_matching_rules_are_selected(tmp_path: Path):
    rules_dir = tmp_path / ".roborak" / "rules"
    write(rules_dir, "php.md", "---\nid: php-only\npaths: ['**/*.php']\n---\nPHP rule.")
    write(rules_dir, "py.md", "---\nid: py-only\npaths: ['**/*.py']\n---\nPython rule.")
    rules = load_rules(tmp_path, ".roborak/rules")

    changeset = ChangeSet(files=[ChangedFile(path="src/app.py", language="python")])
    assert [r.id for r in matching_rules(rules, changeset)] == ["py-only"]


def test_matched_rules_are_ordered_by_severity(tmp_path: Path):
    rules_dir = tmp_path / ".roborak" / "rules"
    write(rules_dir, "a.md", "---\nid: minor-one\nseverity: minor\n---\nX.")
    write(rules_dir, "b.md", "---\nid: critical-one\nseverity: critical\n---\nY.")
    rules = load_rules(tmp_path, ".roborak/rules")

    changeset = ChangeSet(files=[ChangedFile(path="a.py")])
    assert [r.id for r in matching_rules(rules, changeset)] == ["critical-one", "minor-one"]


def test_rules_for_prompt_shape(tmp_path: Path):
    rule = parse_rule(write(tmp_path, "r.md", FULL_RULE))
    flattened = rules_for_prompt([rule])
    assert flattened == [
        {
            "id": "roborak/no-raw-sql",
            "severity": "critical",
            "category": "security",
            "body": "Never build SQL by string concatenation. Use bound parameters.",
        }
    ]


# -- integration with the review ------------------------------------------


def test_matching_rules_reach_the_prompt(tmp_path: Path):
    from roborak.analysis.reviewer import Reviewer
    from roborak.core.config import Config
    from tests.test_pipeline import StubLLM, make_changeset

    write(tmp_path / ".roborak" / "rules", "sql.md", FULL_RULE)
    llm = StubLLM(reply="findings: []")
    Reviewer(config=Config(), repo=tmp_path, llm=llm).review(make_changeset())

    assert "roborak/no-raw-sql" in llm.user
    assert "Never build SQL by string concatenation" in llm.user


def test_non_matching_rules_stay_out_of_the_prompt(tmp_path: Path):
    """Token cost must stay flat as a rule set grows."""
    from roborak.analysis.reviewer import Reviewer
    from roborak.core.config import Config
    from tests.test_pipeline import StubLLM, make_changeset

    write(
        tmp_path / ".roborak" / "rules",
        "go.md",
        "---\nid: go-only\npaths: ['**/*.go']\n---\nA rule about Go.",
    )
    llm = StubLLM(reply="findings: []")
    Reviewer(config=Config(), repo=tmp_path, llm=llm).review(make_changeset())

    assert "go-only" not in llm.user
    assert "A rule about Go" not in llm.user


def test_a_yaml_boolean_id_falls_back_to_the_filename(tmp_path: Path, caplog):
    """PyYAML is YAML 1.1: bare `on`/`off`/`yes`/`no` parse as booleans."""
    rule = parse_rule(write(tmp_path, "on.md", "---\nid: on\n---\nApply me."))
    assert rule.id == "on"
    assert "unusable id" in caplog.text


def test_a_quoted_id_survives(tmp_path: Path):
    rule = parse_rule(write(tmp_path, "x.md", "---\nid: 'off'\n---\nBody."))
    assert rule.id == "off"
