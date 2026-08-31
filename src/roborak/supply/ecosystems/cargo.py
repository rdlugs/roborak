"""Rust: ``Cargo.toml`` and ``Cargo.lock``, both TOML."""

from __future__ import annotations

import re
import tomllib

from roborak.supply.ecosystems.base import Ecosystem, Package, semver_satisfies

_SECTIONS = ("dependencies", "dev-dependencies", "build-dependencies")


def parse_manifest(text: str) -> dict[str, Package]:
    """``Cargo.toml``, including the workspace table a workspace root uses."""
    data = tomllib.loads(text)
    packages: dict[str, Package] = {}
    _add_sections(data, packages)
    workspace = data.get("workspace")
    if isinstance(workspace, dict):
        _add_sections(workspace, packages)
    return packages


def _add_sections(table: dict[str, object], into: dict[str, Package]) -> None:
    for section in _SECTIONS:
        block = table.get(section)
        if not isinstance(block, dict):
            continue
        for name, spec in block.items():
            if isinstance(spec, dict):
                source = str(spec.get("git") or spec.get("path") or spec.get("registry") or "")
                ref = str(spec.get("rev") or spec.get("branch") or spec.get("tag") or "")
                into[str(name)] = Package(
                    version=str(spec.get("version") or ""), source=source, direct=True, ref=ref
                )
            else:
                into[str(name)] = Package(version=str(spec), direct=True)


def parse_lock(text: str) -> dict[str, Package]:
    """``Cargo.lock``: a ``[[package]]`` array with ``checksum`` and ``source``.

    A path dependency has no ``source`` and no ``checksum`` by design, so its
    absence is only interesting when a package *had* one and stopped having it --
    which is exactly what the delta compares.
    """
    data = tomllib.loads(text)
    entries = data.get("package")
    if not isinstance(entries, list):
        return {}
    packages: dict[str, Package] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "")
        if not name:
            continue
        source = str(entry.get("source") or "")
        packages[name] = Package(
            version=str(entry.get("version") or ""),
            source=source,
            integrity=str(entry.get("checksum") or ""),
            ref=source.rpartition("#")[2] if source.startswith("git+") else "",
        )
    return packages


def _version_satisfies(constraint: str, version: str) -> bool | None:
    """Cargo treats a bare version as a caret range, unlike npm's wildcard form."""
    stripped = constraint.strip()
    if re.fullmatch(r"v?\d+(?:\.\d+){0,2}", stripped):
        stripped = f"^{stripped}"
    return semver_satisfies(stripped, version)


CARGO = Ecosystem(
    name="cargo",
    manifests=("Cargo.toml",),
    locks=("Cargo.lock",),
    parse_manifest=parse_manifest,
    parse_lock=parse_lock,
    version_satisfies=_version_satisfies,
)
