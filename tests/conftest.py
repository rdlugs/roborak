import pytest


@pytest.fixture(autouse=True)
def deterministic_ci_environment(monkeypatch):
    """Tests opt into CI behavior explicitly instead of inheriting the runner."""
    monkeypatch.delenv("CI", raising=False)


@pytest.fixture(autouse=True)
def isolated_user_config(tmp_path, monkeypatch):
    """Personal settings must not change test behavior or supply credentials."""
    monkeypatch.setattr(
        "roborak.core.config.USER_CONFIG_PATH",
        tmp_path / "home" / ".config" / "roborak" / ".roborak.yaml",
    )
