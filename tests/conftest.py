import pytest


@pytest.fixture(autouse=True)
def deterministic_ci_environment(monkeypatch):
    """Tests opt into CI behavior explicitly instead of inheriting the runner."""
    monkeypatch.delenv("CI", raising=False)
