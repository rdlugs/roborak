"""LiteLLM wrapper.

Keeping every model call behind this one class means the rest of roborak never
imports litellm, and swapping providers is a config change rather than a code
change. Anthropic, OpenAI, Gemini, Bedrock, Vertex, Ollama and any
OpenAI-compatible endpoint are reachable through the same ``model`` string.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field

from roborak.core.config import LLMConfig

log = logging.getLogger(__name__)

_FALLBACK_CONTEXT = 120_000


class LLMError(RuntimeError):
    """A model call failed in a way the user needs to hear about."""


@dataclass
class LLMResponse:
    text: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: int = 0
    cost_usd: float | None = None

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass
class LLMClient:
    config: LLMConfig
    _litellm: object | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        import litellm

        litellm.suppress_debug_info = True
        litellm.drop_params = True
        self._litellm = litellm

    def complete(self, system: str, user: str) -> LLMResponse:
        """Run one completion, falling back through ``fallback_models`` on failure."""
        models = [self.config.model, *self.config.fallback_models]
        last_error: Exception | None = None

        for model in models:
            try:
                return self._call(model, system, user)
            except Exception as exc:  # noqa: BLE001 - provider errors are unbounded
                log.warning("model %s failed: %s", model, exc)
                last_error = exc

        raise LLMError(
            f"All models failed ({', '.join(models)}). Last error: {last_error}"
        ) from last_error

    def _call(self, model: str, system: str, user: str) -> LLMResponse:
        assert self._litellm is not None
        started = time.monotonic()
        response = self._litellm.completion(  # type: ignore[attr-defined]
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=self.config.temperature,
            max_tokens=self.config.max_tokens,
            timeout=self.config.timeout_seconds,
            num_retries=self.config.max_retries,
            api_key=self._key_for(model),
            api_base=self.config.api_base,
        )
        text = response.choices[0].message.content or ""
        usage = getattr(response, "usage", None)
        hidden = getattr(response, "_hidden_params", None) or {}
        raw_cost = hidden.get("response_cost") if isinstance(hidden, dict) else None
        return LLMResponse(
            text=text,
            model=model,
            prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
            latency_ms=round((time.monotonic() - started) * 1000),
            cost_usd=float(raw_cost) if isinstance(raw_cost, int | float) else None,
        )

    def _key_for(self, model: str) -> str | None:
        """The configured key for this model's provider, if there is one.

        ``None`` leaves litellm to find the key in the environment as before.
        """
        key = self.config.api_keys.get(provider_of(model))
        return key.get_secret_value() if key else None

    def count_tokens(self, text: str) -> int:
        """Token count for the configured model, with a cheap character-based fallback."""
        assert self._litellm is not None
        try:
            return int(self._litellm.token_counter(model=self.config.model, text=text))  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 - tokenizer may be unavailable offline
            return len(text) // 4

    @property
    def context_budget(self) -> int:
        """How many prompt tokens we are willing to spend."""
        if self.config.context_budget:
            return self.config.context_budget
        assert self._litellm is not None
        try:
            info = self._litellm.get_model_info(self.config.model)  # type: ignore[attr-defined]
            window = int(info.get("max_input_tokens") or 0)
        except Exception:  # noqa: BLE001 - unknown model names are common
            window = 0
        window = window or _FALLBACK_CONTEXT
        return max(8_000, window - self.config.max_tokens - 4_000)


def missing_credentials(model: str, llm: LLMConfig | None = None) -> str | None:
    """Return the env var the user still needs to set, if we can tell.

    Better to say "set ANTHROPIC_API_KEY" up front than to surface a provider's
    auth error after the user has waited for a diff to be assembled.

    A key in ``llm.api_keys`` answers the question already, and an ``api_base``
    pointing at a proxy or a local Ollama often needs no provider key at all --
    demanding one in either case would be a false alarm.
    """
    provider = provider_of(model)
    if llm is not None and (llm.api_base or provider in llm.api_keys):
        return None
    required = {
        "anthropic": "ANTHROPIC_API_KEY",
        "openai": "OPENAI_API_KEY",
        "gemini": "GEMINI_API_KEY",
        "mistral": "MISTRAL_API_KEY",
        "groq": "GROQ_API_KEY",
        "deepseek": "DEEPSEEK_API_KEY",
        "openrouter": "OPENROUTER_API_KEY",
        "xai": "XAI_API_KEY",
    }.get(provider)
    if required and not os.getenv(required):
        return required
    return None


def provider_of(model: str) -> str:
    """The provider a model string names, from its prefix or its shape."""
    return model.split("/", 1)[0] if "/" in model else _infer_provider(model)


def _infer_provider(model: str) -> str:
    lowered = model.lower()
    if lowered.startswith("claude"):
        return "anthropic"
    if lowered.startswith(("gpt-", "o1", "o3", "o4")):
        return "openai"
    if lowered.startswith("gemini"):
        return "gemini"
    return ""
