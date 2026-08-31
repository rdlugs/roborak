"""PHP: ``composer.json`` and ``composer.lock``, both JSON."""

from __future__ import annotations

import json
import re
from typing import Any

from roborak.supply.ecosystems.base import Ecosystem, Package, semver_satisfies

_SECTIONS = ("require", "require-dev")


def parse_manifest(text: str) -> dict[str, Package]:
    """``composer.json``, plus the ``repositories`` list that redirects resolution.

    A custom repository entry is recorded against every package it could serve --
    there is no per-package attribution in composer's manifest -- so it surfaces as
    a source on the project rather than being lost.
    """
    data = json.loads(text)
    if not isinstance(data, dict):
        return {}
    packages: dict[str, Package] = {}
    for section in _SECTIONS:
        block = data.get(section)
        if not isinstance(block, dict):
            continue
        for name, constraint in block.items():
            if str(name) in {"php"} or str(name).startswith("ext-"):
                continue
            packages[str(name)] = Package(version=str(constraint), direct=True)

    repositories = data.get("repositories")
    if repositories:
        packages["(composer repositories)"] = Package(
            source=json.dumps(repositories, sort_keys=True)[:200], direct=True
        )
    return packages


def parse_lock(text: str) -> dict[str, Package]:
    """``composer.lock``: ``packages`` and ``packages-dev`` arrays."""
    data = json.loads(text)
    if not isinstance(data, dict):
        return {}
    packages: dict[str, Package] = {}
    for section in ("packages", "packages-dev"):
        entries = data.get(section)
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name") or "")
            if not name:
                continue
            packages[name] = Package(
                version=str(entry.get("version") or ""),
                source=_source_of(entry),
                integrity=_integrity_of(entry),
                ref=_ref_of(entry),
            )
    return packages


def _dist(entry: dict[str, Any]) -> dict[str, Any]:
    dist = entry.get("dist")
    return dist if isinstance(dist, dict) else {}


def _source_of(entry: dict[str, Any]) -> str:
    source = entry.get("source")
    if isinstance(source, dict) and (url := source.get("url")):
        return str(url)
    return str(_dist(entry).get("url") or "")


def _integrity_of(entry: dict[str, Any]) -> str:
    return str(_dist(entry).get("shasum") or "")


def _ref_of(entry: dict[str, Any]) -> str:
    """The commit a package is pinned to, when it came from a repository."""
    source = entry.get("source")
    if isinstance(source, dict) and source.get("type") == "git":
        return str(source.get("reference") or "")
    return ""


def _version_satisfies(constraint: str, version: str) -> bool | None:
    """Composer bare full versions are exact; ambiguous partials stay unknown."""
    stripped = constraint.strip()
    if re.fullmatch(r"~v?\d+\.\d+", stripped):
        # Composer advances the major here, unlike npm/Cargo's shared tilde rule.
        return None
    if re.fullmatch(r"v?\d+\.\d+\.\d+", stripped):
        return semver_satisfies(f"={stripped}", version)
    if re.fullmatch(r"v?\d+(?:\.\d+)?", stripped):
        return None
    return semver_satisfies(stripped, version)


COMPOSER = Ecosystem(
    name="composer",
    manifests=("composer.json",),
    locks=("composer.lock",),
    parse_manifest=parse_manifest,
    parse_lock=parse_lock,
    version_satisfies=_version_satisfies,
)
