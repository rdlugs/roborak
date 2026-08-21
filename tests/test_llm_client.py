"""Credential and endpoint resolution in the LiteLLM wrapper.

There is no live key here, so a fake litellm records what it was handed. What
matters is that the right key reaches the right provider -- the fallback chain
crosses providers, so this cannot be resolved once per session.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest

from roborak.core.config import LLMConfig
from roborak.llm.client import LLMClient, LLMError, missing_credentials, provider_of


@dataclass
class FakeLiteLLM:
    """Records every completion call; fails for models in ``failing``."""

    failing: set[str] = field(default_factory=set)
    calls: list[dict[str, Any]] = field(default_factory=list)

    def completion(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if kwargs["model"] in self.failing:
            raise RuntimeError("provider said no")
        message = SimpleNamespace(content="ok")
        return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=None)


@pytest.fixture
def client(monkeypatch):
    """An ``LLMClient`` whose litellm import is replaced by the fake."""

    def build(config: LLMConfig, failing: set[str] | None = None) -> tuple[LLMClient, FakeLiteLLM]:
        fake = FakeLiteLLM(failing=failing or set())
        monkeypatch.setattr(LLMClient, "__post_init__", lambda self: None)
        instance = LLMClient(config)
        instance._litellm = fake
        return instance, fake

    return build


def test_configured_key_follows_the_model_across_providers(client):
    """The openai fallback must get the openai key, not the anthropic one."""
    config = LLMConfig(
        model="anthropic/claude-opus-4-8",
        fallback_models=["openai/gpt-4o-mini"],
        api_keys={"anthropic": "sk-ant-test", "openai": "sk-openai-test"},
    )
    instance, fake = client(config, failing={"anthropic/claude-opus-4-8"})

    instance.complete("sys", "user")

    assert [call["api_key"] for call in fake.calls] == ["sk-ant-test", "sk-openai-test"]


def test_unconfigured_provider_passes_no_key(client):
    """litellm's own environment lookup has to stay intact."""
    config = LLMConfig(model="gemini/gemini-2.0-flash", api_keys={"anthropic": "sk-ant-test"})
    instance, fake = client(config)

    instance.complete("sys", "user")

    assert fake.calls[0]["api_key"] is None


def test_api_base_reaches_the_provider_call(client):
    config = LLMConfig(model="ollama/llama3", api_base="http://localhost:11434")
    instance, fake = client(config)

    instance.complete("sys", "user")

    assert fake.calls[0]["api_base"] == "http://localhost:11434"


def test_every_model_failing_still_raises(client):
    config = LLMConfig(model="anthropic/claude-opus-4-8", fallback_models=["openai/gpt-4o-mini"])
    instance, _ = client(config, failing={"anthropic/claude-opus-4-8", "openai/gpt-4o-mini"})

    with pytest.raises(LLMError):
        instance.complete("sys", "user")


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("anthropic/claude-opus-4-8", "anthropic"),
        ("claude-sonnet-5", "anthropic"),
        ("gpt-4o-mini", "openai"),
        ("gemini-2.0-flash", "gemini"),
        ("some-local-thing", ""),
    ],
)
def test_provider_of(model: str, expected: str):
    assert provider_of(model) == expected


def test_configured_key_satisfies_the_credential_check(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    config = LLMConfig(model="anthropic/claude-opus-4-8", api_keys={"anthropic": "sk-ant-test"})
    assert missing_credentials(config.model, config) is None


def test_api_base_alone_satisfies_the_credential_check(monkeypatch):
    """A proxy or local Ollama often needs no provider key at all."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    config = LLMConfig(model="anthropic/claude-opus-4-8", api_base="http://localhost:4000")
    assert missing_credentials(config.model, config) is None


def test_env_var_is_still_named_when_nothing_is_configured(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    config = LLMConfig(model="anthropic/claude-opus-4-8")
    assert missing_credentials(config.model, config) == "ANTHROPIC_API_KEY"
    assert missing_credentials(config.model) == "ANTHROPIC_API_KEY"


def test_a_key_for_another_provider_does_not_count(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    config = LLMConfig(model="openai/gpt-4o", api_keys={"anthropic": "sk-ant-test"})
    assert missing_credentials(config.model, config) == "OPENAI_API_KEY"
