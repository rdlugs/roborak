"""Layered configuration.

Precedence, highest first: CLI flags, environment (``ROBORAK_*``), the project's
``.roborak.yaml``, the user's ``~/.config/roborak/config.yaml``, then defaults.
The shape follows Kodus' config so the concepts are familiar: categories, a
severity floor, path ignores, and a static-analysis section.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, SecretStr, field_validator

from roborak.core.severity import Category, Severity

log = logging.getLogger(__name__)

PROJECT_CONFIG_NAMES = (".roborak.yaml", ".roborak.yml")
USER_CONFIG_PATH = Path.home() / ".config" / "roborak" / "config.yaml"

DEFAULT_IGNORE_PATHS = [
    "**/*.lock",
    "**/*.min.js",
    "**/*.min.css",
    "**/*.map",
    "**/vendor/**",
    "**/node_modules/**",
    "**/dist/**",
    "**/build/**",
    "**/__pycache__/**",
    "**/*.snap",
    "**/package-lock.json",
    "**/yarn.lock",
    "**/poetry.lock",
    "**/uv.lock",
    "**/composer.lock",
]


class ReviewConfig(BaseModel):
    categories: list[Category] = Field(
        default_factory=lambda: [
            Category.SECURITY,
            Category.BUG,
            Category.PERFORMANCE,
            Category.LOGIC,
        ]
    )
    severity_floor: Severity = Severity.MINOR
    max_findings: int = 25
    committable_suggestions: bool = True
    min_confidence: float = 0.5
    full_file: bool = False
    """Allow findings on lines the change did not touch. Off by default: it is the
    main source of noise, since untouched code is not what the author asked about."""

    check_requirements: bool = True
    """Let the model report requirements the change does not implement. Only ever
    consulted when ``--issue`` supplied something to check against."""


class StaticConfig(BaseModel):
    enabled: bool = True
    tools: list[str] | None = None
    """``None`` means autodetect whatever is on PATH."""

    timeout_seconds: int = 90
    feed_to_llm: bool = True
    max_findings_in_prompt: int = 40


class LLMConfig(BaseModel):
    model: str = "anthropic/claude-sonnet-5"
    fallback_models: list[str] = Field(default_factory=list)
    temperature: float = 0.2
    max_tokens: int = 8000
    context_budget: int | None = None
    """Prompt token ceiling. ``None`` derives it from the model's known window."""

    api_keys: dict[str, SecretStr] = Field(default_factory=dict)
    """Provider name to key, e.g. ``{"anthropic": "sk-ant-..."}``. Takes precedence
    over the provider's environment variable; omit a provider to keep using it.
    ``SecretStr`` keeps the value out of ``config show``, logs and tracebacks."""

    api_base: str | None = None
    """Endpoint override applied to every call: an OpenAI-compatible proxy, an
    Azure deployment, or a local Ollama."""

    timeout_seconds: int = 180
    max_retries: int = 2


class OutputConfig(BaseModel):
    walkthrough: bool = True
    """Spend a second model call on an overview of the change. Worth it for the
    summary comment a review posts; turn it off to halve the cost of a run."""

    confirm_post: bool = True
    """At the end of an interactive review, offer to publish the result. Only ever
    consulted on a terminal: a script or a CI job is never prompted."""

    panels: bool = False
    """Print the review as rich panels instead of the markdown report. The panels
    show each finding's code in context, read from the working tree, which the
    report cannot do -- but they are not what gets published."""


class ForgeConfig(BaseModel):
    tokens: dict[str, SecretStr] = Field(default_factory=dict)
    """Provider name to token, e.g. ``{"gitlab": "glpat-..."}``. Takes precedence
    over the provider's environment variable; omit a provider to keep using it.
    ``SecretStr`` keeps the value out of ``config show``, logs and tracebacks."""

    hosts: dict[str, str] = Field(default_factory=dict)
    """Provider name to the instance's domain, e.g. ``{"gitlab": "gitlab.acme.com"}``.
    Only consulted when the repository's git remote does not answer the question, so a
    domain set user-wide can never hijack a repo whose remote says otherwise."""

    @field_validator("hosts")
    @classmethod
    def _normalise_hosts(cls, hosts: dict[str, str]) -> dict[str, str]:
        """Reduce each value to ``host[:port]``, keeping only a non-default scheme.

        Storing the bare netloc is what lets ``Target.host`` stay the plain domain
        everywhere it is shown or used as a key, with ``https`` simply assumed.
        """
        cleaned: dict[str, str] = {}
        for provider, raw in hosts.items():
            value = raw.strip().rstrip("/")
            scheme = ""
            for prefix in ("https://", "http://"):
                if value.lower().startswith(prefix):
                    scheme = "" if prefix == "https://" else prefix
                    value = value[len(prefix) :]
                    break
            if not value:
                raise ValueError(f"forge.hosts.{provider} is empty.")
            if "/" in value:
                raise ValueError(
                    f"forge.hosts.{provider} must be a domain like 'gitlab.acme.com', "
                    f"optionally with a scheme and port, not a URL path: {raw!r}."
                )
            cleaned[provider] = scheme + value
        return cleaned


class Config(BaseModel):
    version: int = 1
    review: ReviewConfig = Field(default_factory=ReviewConfig)
    static: StaticConfig = Field(default_factory=StaticConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    forge: ForgeConfig = Field(default_factory=ForgeConfig)
    output: OutputConfig = Field(default_factory=OutputConfig)
    ignore_paths: list[str] = Field(default_factory=lambda: list(DEFAULT_IGNORE_PATHS))
    rules_dir: str = ".roborak/rules"
    language_instructions: dict[str, str] = Field(default_factory=dict)
    """Extra prompt guidance keyed by language, e.g. ``{"php": "This is Laravel 10."}``."""

    @property
    def model(self) -> str:
        return self.llm.model


def load_config(repo: Path, explicit_path: Path | None = None) -> Config:
    """Merge every configuration layer into one ``Config``."""
    layers: list[dict[str, Any]] = []

    if USER_CONFIG_PATH.is_file():
        layers.append(_read_yaml(USER_CONFIG_PATH))

    if explicit_path is not None:
        if not explicit_path.is_file():
            raise FileNotFoundError(f"Config file not found: {explicit_path}")
        layers.append(_read_yaml(explicit_path))
    else:
        for name in PROJECT_CONFIG_NAMES:
            candidate = repo / name
            if candidate.is_file():
                layers.append(_read_yaml(candidate))
                break

    layers.append(_env_layer())

    merged: dict[str, Any] = {}
    for layer in layers:
        merged = _deep_merge(merged, layer)
    return Config.model_validate(merged)


def _read_yaml(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a YAML mapping, got {type(data).__name__}.")
    _warn_if_others_can_read_keys(path, data)
    return data


def _warn_if_others_can_read_keys(path: Path, data: dict[str, Any]) -> None:
    """A file holding literal secrets has no business being readable by others."""
    llm = data.get("llm")
    forge = data.get("forge")
    holds_secrets = (isinstance(llm, dict) and bool(llm.get("api_keys"))) or (
        isinstance(forge, dict) and bool(forge.get("tokens"))
    )
    if not holds_secrets:
        return
    try:
        mode = path.stat().st_mode
    except OSError:  # racing a delete is not worth failing the load over
        return
    if mode & 0o077:
        log.warning("%s holds secrets and is readable by other accounts; chmod 600 it.", path)


def _env_layer() -> dict[str, Any]:
    """Map the handful of env vars worth supporting onto the config tree."""
    layer: dict[str, Any] = {}
    if model := os.getenv("ROBORAK_MODEL"):
        layer.setdefault("llm", {})["model"] = model
    if floor := os.getenv("ROBORAK_SEVERITY_FLOOR"):
        layer.setdefault("review", {})["severity_floor"] = floor
    forge: dict[str, dict[str, str]] = {}
    for provider in ("gitlab", "github"):
        if token := os.getenv(f"ROBORAK_{provider.upper()}_TOKEN"):
            forge.setdefault("tokens", {})[provider] = token
        # A host from the environment is still only a fallback for the git remote:
        # it is config like any other, it just arrives by a higher-priority layer.
        if host := os.getenv(f"ROBORAK_{provider.upper()}_HOST"):
            forge.setdefault("hosts", {})[provider] = host
    if forge:
        layer["forge"] = forge
    if (static_off := os.getenv("ROBORAK_NO_STATIC")) and static_off not in {"0", "false", ""}:
        layer.setdefault("static", {})["enabled"] = False
    return layer


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Merge mappings recursively; lists and scalars are replaced, not appended."""
    out = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out
