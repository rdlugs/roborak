"""Layered configuration.

Precedence, highest first: CLI flags, environment (``ROBORAK_*``), the project's
``.roborak.yaml``, the user's ``~/.config/roborak/config.yaml``, then defaults.
The shape follows Kodus' config so the concepts are familiar: categories, a
severity floor, path ignores, and a static-analysis section.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from roborak.core.severity import Category, Severity

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

    timeout_seconds: int = 180
    max_retries: int = 2


class Config(BaseModel):
    version: int = 1
    review: ReviewConfig = Field(default_factory=ReviewConfig)
    static: StaticConfig = Field(default_factory=StaticConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
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
    return data


def _env_layer() -> dict[str, Any]:
    """Map the handful of env vars worth supporting onto the config tree."""
    layer: dict[str, Any] = {}
    if model := os.getenv("ROBORAK_MODEL"):
        layer.setdefault("llm", {})["model"] = model
    if floor := os.getenv("ROBORAK_SEVERITY_FLOOR"):
        layer.setdefault("review", {})["severity_floor"] = floor
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
