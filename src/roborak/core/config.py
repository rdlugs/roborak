"""Layered configuration.

Precedence, highest first: CLI flags, environment (``ROBORAK_*``), the project's
``.roborak.yaml``, the user's ``~/.config/roborak/config.yaml``, then defaults.
The shape is four sections: categories, a severity floor, path ignores, and a
static-analysis section.
"""

from __future__ import annotations

import logging
import os
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

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


class ConfigModel(BaseModel):
    """Configuration is user-authored, so typos must fail instead of disappearing."""

    model_config = ConfigDict(extra="forbid")


class StaticExecution(StrEnum):
    AUTO = "auto"
    TRUSTED = "trusted"
    OFF = "off"


class ReviewConfig(ConfigModel):
    categories: list[Category] = Field(
        default_factory=lambda: [
            Category.SECURITY,
            Category.BUG,
            Category.PERFORMANCE,
            Category.LOGIC,
        ]
    )
    severity_floor: Severity = Severity.MINOR
    max_findings: int = Field(default=25, ge=1)
    committable_suggestions: bool = True
    min_confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    full_file: bool = False
    """Allow findings on lines the change did not touch. Off by default: it is the
    main source of noise, since untouched code is not what the author asked about."""

    check_requirements: bool = True
    """Let the model report requirements the change does not implement. Only ever
    consulted when ``--issue`` supplied something to check against."""

    include_discussions: bool = True
    """Use relevant forge discussion as bounded, untrusted review context."""


class StaticConfig(ConfigModel):
    enabled: bool = True
    execution: StaticExecution = StaticExecution.AUTO
    """``auto`` is direct for local work and sandboxed in CI; ``trusted`` is an
    explicit opt-in to direct execution, and ``off`` disables static tools."""
    tools: list[str] | None = None
    """``None`` means autodetect whatever is on PATH."""

    timeout_seconds: int = Field(default=90, ge=1)
    feed_to_llm: bool = True
    max_findings_in_prompt: int = Field(default=40, ge=0)


class LLMConfig(ConfigModel):
    model: str = "anthropic/claude-sonnet-5"
    fallback_models: list[str] = Field(default_factory=list)
    temperature: float = Field(default=0.2, ge=0.0, le=2.0)
    max_tokens: int = Field(default=8000, ge=1)
    context_budget: int | None = Field(default=None, ge=1000)
    """Prompt token ceiling. ``None`` derives it from the model's known window."""

    api_keys: dict[str, SecretStr] = Field(default_factory=dict)
    """Provider name to key, e.g. ``{"anthropic": "sk-ant-..."}``. Takes precedence
    over the provider's environment variable; omit a provider to keep using it.
    ``SecretStr`` keeps the value out of ``config show``, logs and tracebacks."""

    api_base: str | None = None
    """Endpoint override applied to every call: an OpenAI-compatible proxy, an
    Azure deployment, or a local Ollama."""

    timeout_seconds: int = Field(default=180, ge=1)
    max_retries: int = Field(default=2, ge=0)


class OutputConfig(ConfigModel):
    walkthrough: bool = True
    """Spend a second model call on an overview of the change. Worth it for the
    summary comment a review posts; turn it off to halve the cost of a run."""

    confirm_post: bool = True
    """At the end of an interactive review, offer to publish the result. Only ever
    consulted on a terminal: a script or a CI job is never prompted."""

    panels: bool = False
    """Print the review as rich panels instead of the report -- one finding to a
    bordered panel, which is the denser way to read a long review. Not what gets
    published either way."""

    full: bool = False
    """Show the sections the terminal report leaves out: the agent prompt under
    each finding and the review-info tree. They are written for a machine, and
    opened out on a screen they bury the review. What a reader must not lose --
    an omitted file, a skipped file, an error -- is in the footer regardless."""


class ForgeConfig(ConfigModel):
    tokens: dict[str, SecretStr] = Field(default_factory=dict)
    """Provider name to token, e.g. ``{"gitlab": "glpat-..."}``. Takes precedence
    over the provider's environment variable; omit a provider to keep using it.
    ``SecretStr`` keeps the value out of ``config show``, logs and tracebacks."""

    hosts: dict[str, str] = Field(default_factory=dict)
    """Provider name to the instance's domain, e.g. ``{"gitlab": "gitlab.acme.com"}``.
    Only consulted when the repository's git remote does not answer the question, so a
    domain set user-wide can never hijack a repo whose remote says otherwise."""

    max_recovered_file_bytes: int = Field(default=1_048_576, ge=1)
    """Largest forge-truncated text file we will fetch to reconstruct a patch."""

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


class Config(ConfigModel):
    version: Literal[1] = 1
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
    elif not _in_ci():
        for name in PROJECT_CONFIG_NAMES:
            candidate = repo / name
            if candidate.is_file():
                layers.append(_read_yaml(candidate))
                break
    else:
        log.debug(
            "ignoring working-tree project configuration in CI; pass --config with a trusted "
            "path or use environment/user configuration"
        )

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


_WINDOWS = os.name == "nt"


def _warn_if_others_can_read_keys(path: Path, data: dict[str, Any]) -> None:
    """A file holding literal secrets has no business being readable by others.

    POSIX only. Windows governs access through ACLs and synthesises ``st_mode``
    as 0o666 or 0o444 whatever the ACL actually says, so the group and other bits
    carry no information there -- checking them reports every config as exposed,
    and ``chmod 600`` is not advice a Windows user can act on.
    """
    if _WINDOWS:
        return
    llm = data.get("llm")
    forge = data.get("forge")
    holds_secrets = (isinstance(llm, dict) and bool(llm.get("api_keys"))) or (
        isinstance(forge, dict) and bool(forge.get("tokens"))
    )
    if not holds_secrets:
        return
    try:
        mode = path.stat().st_mode
    except OSError:
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
        if host := os.getenv(f"ROBORAK_{provider.upper()}_HOST"):
            forge.setdefault("hosts", {})[provider] = host
    if forge:
        layer["forge"] = forge
    if (static_off := os.getenv("ROBORAK_NO_STATIC")) and static_off not in {"0", "false", ""}:
        layer.setdefault("static", {})["enabled"] = False
    if execution := os.getenv("ROBORAK_STATIC_EXECUTION"):
        layer.setdefault("static", {})["execution"] = execution
    return layer


def _in_ci() -> bool:
    value = os.getenv("CI", "").strip().lower()
    return value not in {"", "0", "false", "no"}


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Merge mappings recursively; lists and scalars are replaced, not appended."""
    out = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out
