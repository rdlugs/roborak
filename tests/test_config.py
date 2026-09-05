"""Configuration layering."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from roborak.core.config import (
    USER_CONFIG_PATH,
    Config,
    ForgeCheckout,
    ReviewConfig,
    load_config,
    load_verification,
)
from roborak.core.severity import Category, Severity


def test_user_config_default_path():
    assert Path.home() / ".config" / "roborak" / ".roborak.yaml" == USER_CONFIG_PATH


@pytest.mark.parametrize("new_exists", [False, True])
def test_user_config_uses_new_filename_without_legacy_fallback(
    tmp_path: Path, monkeypatch, new_exists: bool
):
    directory = tmp_path / "home" / ".config" / "roborak"
    directory.mkdir(parents=True)
    user = directory / ".roborak.yaml"
    monkeypatch.setattr("roborak.core.config.USER_CONFIG_PATH", user)
    legacy = directory / "config.yaml"
    legacy_text = (
        "llm:\n  model: legacy/model\nreview:\n  max_findings: 3\n"
        "verification:\n  fallback: [legacy-command]\n  timeout_seconds: 7\n"
    )
    legacy.write_text(legacy_text)
    if new_exists:
        user.write_text("llm:\n  model: new/model\nverification:\n  fallback: [new-command]\n")

    config = load_config(tmp_path)
    verification, _, _ = load_verification(tmp_path, ref="")
    defaults = Config()
    assert config.model == ("new/model" if new_exists else defaults.model)
    assert config.review.max_findings == defaults.review.max_findings
    assert verification.fallback == (["new-command"] if new_exists else [])
    assert verification.timeout_seconds == defaults.verification.timeout_seconds
    assert legacy.read_text() == legacy_text


def test_defaults_when_nothing_is_configured(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("roborak.core.config.USER_CONFIG_PATH", tmp_path / "absent.yaml")
    config = load_config(tmp_path)
    assert config.model == "anthropic/claude-sonnet-5"
    assert config.review.severity_floor is Severity.MINOR
    assert config.review.require_evidence
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


def test_reliability_is_a_configurable_category(tmp_path: Path, monkeypatch):
    """A project that does not want rollout review can turn the category off."""
    monkeypatch.setattr("roborak.core.config.USER_CONFIG_PATH", tmp_path / "absent.yaml")
    project = tmp_path / ".roborak.yaml"
    project.write_text("review:\n  categories: [bug, reliability]\n")
    # Explicit, so the assertion holds whether or not the suite runs in CI.
    config = load_config(tmp_path, project)
    assert config.review.categories == [Category.BUG, Category.RELIABILITY]


def test_reliability_is_reviewed_by_default():
    """Off by default would make the whole checklist dead code for most users."""
    assert Category.RELIABILITY in ReviewConfig().categories


@pytest.mark.parametrize("filename", [".roborak.yaml", ".roborak.yml"])
def test_ci_ignores_working_tree_config_but_accepts_explicit_config(
    tmp_path: Path, monkeypatch, filename: str
):
    monkeypatch.setattr("roborak.core.config.USER_CONFIG_PATH", tmp_path / "absent.yaml")
    project = tmp_path / filename
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


def test_canonical_project_file_wins_without_merging_alternate(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("roborak.core.config.USER_CONFIG_PATH", tmp_path / "absent.yaml")
    (tmp_path / ".roborak.yaml").write_text("llm:\n  model: yaml/model\n")
    (tmp_path / ".roborak.yml").write_text("llm:\n  model: yml/model\n  temperature: 0.7\n")

    config = load_config(tmp_path)
    assert config.model == "yaml/model"
    assert config.llm.temperature == Config().llm.temperature


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
    # Force the POSIX branch: a Windows st_mode is 0o666 regardless, which still
    # trips the check, so the warning half of this is meaningful on both. Patch the
    # module constant, never os.name -- that object is shared with every other module.
    monkeypatch.setattr("roborak.core.config._WINDOWS", False)
    project = tmp_path / ".roborak.yaml"
    project.write_text("llm:\n  api_keys:\n    anthropic: sk-ant-exposed\n")
    project.chmod(0o644)

    with caplog.at_level("WARNING"):
        load_config(tmp_path)
    assert "chmod 600" in caplog.text

    if os.name == "nt":  # chmod cannot clear those bits on Windows
        return
    caplog.clear()
    project.chmod(0o600)
    with caplog.at_level("WARNING"):
        load_config(tmp_path)
    assert not caplog.text


def test_key_file_permissions_are_not_judged_on_windows(tmp_path: Path, monkeypatch, caplog):
    """Windows synthesises st_mode, so the POSIX check would flag every config."""
    monkeypatch.setattr("roborak.core.config.USER_CONFIG_PATH", tmp_path / "absent.yaml")
    monkeypatch.setattr("roborak.core.config._WINDOWS", True)
    project = tmp_path / ".roborak.yaml"
    project.write_text("llm:\n  api_keys:\n    anthropic: sk-ant-exposed\n")
    project.chmod(0o644)

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


def test_require_evidence_can_be_turned_off_in_a_project_config(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("roborak.core.config.USER_CONFIG_PATH", tmp_path / "absent.yaml")
    (tmp_path / ".roborak.yaml").write_text("review:\n  require_evidence: false\n")
    assert load_config(tmp_path).review.require_evidence is False


def test_supply_chain_defaults_and_strictness(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("roborak.core.config.USER_CONFIG_PATH", tmp_path / "absent.yaml")
    config = load_config(tmp_path)
    assert config.supply_chain.enabled
    assert config.supply_chain.feed_to_llm
    assert config.supply_chain.max_changes == 40
    assert config.supply_chain.token_budget == 1200

    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Config.model_validate({"supply_chain": {"max_assets": 0}})
    with pytest.raises(ValidationError):
        Config.model_validate({"supply_chain": {"enabeld": True}})


def test_supply_chain_can_be_switched_off_by_env(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("roborak.core.config.USER_CONFIG_PATH", tmp_path / "absent.yaml")
    monkeypatch.setenv("ROBORAK_NO_SUPPLY_CHAIN", "1")
    assert load_config(tmp_path).supply_chain.enabled is False
    monkeypatch.setenv("ROBORAK_NO_SUPPLY_CHAIN", "0")
    assert load_config(tmp_path).supply_chain.enabled is True


def test_the_forge_checkout_can_be_switched_off_by_env(tmp_path: Path, monkeypatch):
    """CI is where a network fetch is least wanted and the repo config least editable."""
    monkeypatch.setattr("roborak.core.config.USER_CONFIG_PATH", tmp_path / "absent.yaml")
    assert load_config(tmp_path).impact.forge_checkout is ForgeCheckout.AUTO
    monkeypatch.setenv("ROBORAK_IMPACT_FORGE_CHECKOUT", "off")
    assert load_config(tmp_path).impact.forge_checkout is ForgeCheckout.OFF


def test_an_unknown_forge_checkout_mode_is_rejected():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Config.model_validate({"impact": {"forge_checkout": "sometimes"}})


def test_the_shipped_template_matches_the_config_model(tmp_path: Path, monkeypatch):
    """A template key the model rejects is a config file that fails on first use."""
    from roborak.core.config import PROJECT_CONFIG_NAMES

    template = Path(__file__).resolve().parents[1] / "src" / "roborak" / "config_template.yaml"
    (tmp_path / PROJECT_CONFIG_NAMES[0]).write_text(template.read_text())
    monkeypatch.setattr("roborak.core.config.USER_CONFIG_PATH", tmp_path / "absent.yaml")
    monkeypatch.delenv("CI", raising=False)
    assert load_config(tmp_path).supply_chain.max_changes == 40
