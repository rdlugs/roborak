"""Configuration layering."""

from __future__ import annotations

from pathlib import Path

import pytest

from roborak.core.config import Config, load_config
from roborak.core.severity import Category, Severity


def test_defaults_when_nothing_is_configured(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("roborak.core.config.USER_CONFIG_PATH", tmp_path / "absent.yaml")
    config = load_config(tmp_path)
    assert config.model == "anthropic/claude-sonnet-5"
    assert config.review.severity_floor is Severity.MINOR
    assert config.static.enabled
    assert "**/node_modules/**" in config.ignore_paths


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
    # Lists replace rather than merge, so the project stays in control of ignores.
    assert config.ignore_paths == ["**/*.generated.ts"]


def test_project_config_merges_nested_keys(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("roborak.core.config.USER_CONFIG_PATH", tmp_path / "absent.yaml")
    (tmp_path / ".roborak.yaml").write_text("llm:\n  temperature: 0.7\n")
    config = load_config(tmp_path)
    # Only temperature was set; the rest of the llm section keeps its defaults.
    assert config.llm.temperature == 0.7
    assert config.llm.model == "anthropic/claude-sonnet-5"


def test_project_config_beats_user_config(tmp_path: Path, monkeypatch):
    user = tmp_path / "user.yaml"
    user.write_text("llm:\n  model: user/model\nreview:\n  max_findings: 5\n")
    monkeypatch.setattr("roborak.core.config.USER_CONFIG_PATH", user)
    (tmp_path / ".roborak.yaml").write_text("llm:\n  model: project/model\n")

    config = load_config(tmp_path)
    assert config.model == "project/model"
    # A user setting the project did not override still applies.
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
