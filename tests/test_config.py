"""Configuration layering."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from roborak.core.config import Config, load_config
from roborak.core.severity import Category, Severity


def test_defaults_when_nothing_is_configured(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("roborak.core.config.USER_CONFIG_PATH", tmp_path / "absent.yaml")
    config = load_config(tmp_path)
    assert config.model == "anthropic/claude-sonnet-5"
    assert config.review.severity_floor is Severity.MINOR
    assert config.static.enabled
    assert "**/node_modules/**" in config.ignore_paths


def test_invalid_numeric_ranges_and_unknown_keys_fail():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Config.model_validate({"review": {"min_confidence": 1.1}})
    with pytest.raises(ValidationError):
        Config.model_validate({"review": {"max_fidings": 10}})


def test_project_config_overrides_defaults(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("roborak.core.config.USER_CONFIG_PATH", tmp_path / "absent.yaml")
    (tmp_path / ".roborak.yaml").write_text(
        "llm:\n"
        "  model: openai/gpt-5\n"
        "review:\n"
        "  severity_floor: major\n"
        "  categories: [security]\n"
        "ignore_paths: ['**/*.generated.ts']\n"
    )
    config = load_config(tmp_path)
    assert config.model == "openai/gpt-5"
    assert config.review.severity_floor is Severity.MAJOR
    assert config.review.categories == [Category.SECURITY]
    assert config.ignore_paths == ["**/*.generated.ts"]


def test_ci_ignores_working_tree_config_but_accepts_explicit_config(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("roborak.core.config.USER_CONFIG_PATH", tmp_path / "absent.yaml")
    project = tmp_path / ".roborak.yaml"
    project.write_text(
        "llm:\n  api_base: https://attacker.example\nstatic:\n  execution: trusted\n"
    )
    monkeypatch.setenv("CI", "true")
    automatic = load_config(tmp_path)
    explicit = load_config(tmp_path, project)
    assert automatic.llm.api_base is None
    assert automatic.static.execution.value == "auto"
    assert explicit.llm.api_base == "https://attacker.example"


def test_project_config_merges_nested_keys(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("roborak.core.config.USER_CONFIG_PATH", tmp_path / "absent.yaml")
    (tmp_path / ".roborak.yaml").write_text("llm:\n  temperature: 0.7\n")
    config = load_config(tmp_path)
    assert config.llm.temperature == 0.7
    assert config.llm.model == "anthropic/claude-sonnet-5"


def test_project_config_beats_user_config(tmp_path: Path, monkeypatch):
    user = tmp_path / "user.yaml"
    user.write_text("llm:\n  model: user/model\nreview:\n  max_findings: 5\n")
    monkeypatch.setattr("roborak.core.config.USER_CONFIG_PATH", user)
    (tmp_path / ".roborak.yaml").write_text("llm:\n  model: project/model\n")

    config = load_config(tmp_path)
    assert config.model == "project/model"
    assert config.review.max_findings == 5


def test_env_beats_files(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("roborak.core.config.USER_CONFIG_PATH", tmp_path / "absent.yaml")
    (tmp_path / ".roborak.yaml").write_text("llm:\n  model: project/model\n")
    monkeypatch.setenv("ROBORAK_MODEL", "env/model")
    monkeypatch.setenv("ROBORAK_SEVERITY_FLOOR", "critical")
    monkeypatch.setenv("ROBORAK_NO_STATIC", "1")

    config = load_config(tmp_path)
    assert config.model == "env/model"
    assert config.review.severity_floor is Severity.CRITICAL
    assert not config.static.enabled


def test_yml_extension_is_accepted(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("roborak.core.config.USER_CONFIG_PATH", tmp_path / "absent.yaml")
    (tmp_path / ".roborak.yml").write_text("llm:\n  model: yml/model\n")
    assert load_config(tmp_path).model == "yml/model"


def test_explicit_path_wins_over_project_file(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("roborak.core.config.USER_CONFIG_PATH", tmp_path / "absent.yaml")
    (tmp_path / ".roborak.yaml").write_text("llm:\n  model: project/model\n")
    explicit = tmp_path / "other.yaml"
    explicit.write_text("llm:\n  model: explicit/model\n")
    assert load_config(tmp_path, explicit).model == "explicit/model"


def test_missing_explicit_path_is_an_error(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        load_config(tmp_path, tmp_path / "nope.yaml")


def test_empty_config_file_is_fine(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("roborak.core.config.USER_CONFIG_PATH", tmp_path / "absent.yaml")
    (tmp_path / ".roborak.yaml").write_text("")
    assert load_config(tmp_path).model == "anthropic/claude-sonnet-5"


def test_non_mapping_config_is_rejected(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("roborak.core.config.USER_CONFIG_PATH", tmp_path / "absent.yaml")
    (tmp_path / ".roborak.yaml").write_text("- just\n- a\n- list\n")
    with pytest.raises(ValueError, match="must contain a YAML mapping"):
        load_config(tmp_path)


def test_severity_ordering():
    assert Severity.CRITICAL.rank > Severity.MAJOR.rank > Severity.MINOR.rank > Severity.INFO.rank
    assert Severity.CRITICAL.at_least(Severity.MINOR)
    assert not Severity.INFO.at_least(Severity.MAJOR)


def test_config_model_shortcut():
    assert Config().model == Config().llm.model
    assert Config().review.include_discussions is True


def test_api_keys_are_secret_and_merge_across_layers(tmp_path: Path, monkeypatch):
    """A user-level key and a project-level key for different providers coexist."""
    user = tmp_path / "user.yaml"
    user.write_text("llm:\n  api_keys:\n    openai: sk-user-openai\n")
    monkeypatch.setattr("roborak.core.config.USER_CONFIG_PATH", user)
    (tmp_path / ".roborak.yaml").write_text(
        "llm:\n  api_keys:\n    anthropic: sk-project-anthropic\n"
    )

    config = load_config(tmp_path)
    assert config.llm.api_keys["anthropic"].get_secret_value() == "sk-project-anthropic"
    assert config.llm.api_keys["openai"].get_secret_value() == "sk-user-openai"


def test_api_keys_are_redacted_when_dumped(tmp_path: Path, monkeypatch):
    """``config show`` dumps the merged config; a real key must not survive it."""
    monkeypatch.setattr("roborak.core.config.USER_CONFIG_PATH", tmp_path / "absent.yaml")
    (tmp_path / ".roborak.yaml").write_text(
        "llm:\n  api_keys:\n    anthropic: sk-ant-do-not-print\n"
    )

    dumped = load_config(tmp_path).model_dump(mode="json")
    assert dumped["llm"]["api_keys"]["anthropic"] == "**********"
    assert "sk-ant-do-not-print" not in yaml.safe_dump(dumped)


def test_api_base_defaults_to_none_and_round_trips(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("roborak.core.config.USER_CONFIG_PATH", tmp_path / "absent.yaml")
    assert load_config(tmp_path).llm.api_base is None

    (tmp_path / ".roborak.yaml").write_text("llm:\n  api_base: http://localhost:11434\n")
    assert load_config(tmp_path).llm.api_base == "http://localhost:11434"


def test_world_readable_key_file_warns(tmp_path: Path, monkeypatch, caplog):
    monkeypatch.setattr("roborak.core.config.USER_CONFIG_PATH", tmp_path / "absent.yaml")
    project = tmp_path / ".roborak.yaml"
    project.write_text("llm:\n  api_keys:\n    anthropic: sk-ant-exposed\n")
    project.chmod(0o644)

    with caplog.at_level("WARNING"):
        load_config(tmp_path)
    assert "chmod 600" in caplog.text

    caplog.clear()
    project.chmod(0o600)
    with caplog.at_level("WARNING"):
        load_config(tmp_path)
    assert not caplog.text


def test_forge_hosts_are_normalised(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("roborak.core.config.USER_CONFIG_PATH", tmp_path / "absent.yaml")
    (tmp_path / ".roborak.yaml").write_text(
        "forge:\n  hosts:\n    gitlab: https://gitlab.acme.com/\n    github: http://gh.local:8080\n"
    )
    hosts = load_config(tmp_path).forge.hosts
    assert hosts == {"gitlab": "gitlab.acme.com", "github": "http://gh.local:8080"}


@pytest.mark.parametrize("value", ["https://example.com/gitlab", "  "])
def test_an_unusable_forge_host_is_a_config_error(value, tmp_path: Path, monkeypatch):
    monkeypatch.setattr("roborak.core.config.USER_CONFIG_PATH", tmp_path / "absent.yaml")
    (tmp_path / ".roborak.yaml").write_text(f"forge:\n  hosts:\n    gitlab: '{value}'\n")
    with pytest.raises(ValueError, match=r"forge\.hosts\.gitlab"):
        load_config(tmp_path)


def test_host_env_var_beats_the_project_file(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("roborak.core.config.USER_CONFIG_PATH", tmp_path / "absent.yaml")
    (tmp_path / ".roborak.yaml").write_text("forge:\n  hosts:\n    gitlab: from-file\n")
    monkeypatch.setenv("ROBORAK_GITLAB_HOST", "from-env")
    assert load_config(tmp_path).forge.hosts["gitlab"] == "from-env"
