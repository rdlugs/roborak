"""npm, yarn and pnpm.

Three lockfile formats for one ecosystem, because the tooling never agreed:
``package-lock.json`` is JSON, ``pnpm-lock.yaml`` and Yarn Berry are YAML, and
Yarn Classic is a bespoke two-space-indented format that predates all of them.
The Classic reader below is a deliberately small lexer -- it reads the entry
headers and the three fields that matter, and ignores everything else.
"""

from __future__ import annotations

import json
import re
from typing import Any

import yaml

from roborak.supply.ecosystems.base import Ecosystem, Package

_DEPENDENCY_SECTIONS = ("dependencies", "devDependencies", "optionalDependencies")

_GIT_SPEC = re.compile(r"^(?:git\+|github:|gitlab:|bitbucket:)", re.IGNORECASE)


def parse_manifest(text: str) -> dict[str, Package]:
    """``package.json``: what the project asks for, before resolution."""
    data = json.loads(text)
    if not isinstance(data, dict):
        return {}
    packages: dict[str, Package] = {}
    for section in _DEPENDENCY_SECTIONS:
        block = data.get(section)
        if not isinstance(block, dict):
            continue
        for name, spec in block.items():
            spec = str(spec)
            packages[str(name)] = Package(
                version=spec,
                source=spec if _GIT_SPEC.match(spec) or "://" in spec else "",
                direct=True,
                ref=_git_ref(spec),
            )
    return packages


def parse_lock(text: str) -> dict[str, Package]:
    """Whichever of the three lockfile formats this text is.

    Sniffed by content rather than by filename so that a repository which renamed
    or vendored its lockfile still parses, and so a single entry point can serve
    all three names.
    """
    stripped = text.lstrip()
    if stripped.startswith("{"):
        return _parse_package_lock(text)
    if "__metadata:" in text or "\n  resolution:" in text:
        return _parse_yarn_berry(text)
    if stripped.startswith("lockfileVersion") or "\npackages:" in text:
        return _parse_pnpm(text)
    return _parse_yarn_classic(text)


def _parse_package_lock(text: str) -> dict[str, Package]:
    """``package-lock.json``, v2/v3 (``packages``) and v1 (``dependencies``)."""
    data = json.loads(text)
    if not isinstance(data, dict):
        return {}
    packages: dict[str, Package] = {}

    entries = data.get("packages")
    if isinstance(entries, dict):
        for key, entry in entries.items():
            if not isinstance(entry, dict) or not key:
                continue
            # "" is the project itself; the rest are "node_modules/<name>", which
            # nests for transitive copies -- the last segment is the package.
            name = str(key).split("node_modules/")[-1]
            packages[name] = Package(
                version=str(entry.get("version") or ""),
                source=str(entry.get("resolved") or ""),
                integrity=str(entry.get("integrity") or ""),
                direct=bool(entry.get("dev") is None and "/" not in key.rstrip("/")),
                ref=_git_ref(str(entry.get("resolved") or "")),
            )

    legacy = data.get("dependencies")
    if isinstance(legacy, dict):
        _walk_v1(legacy, packages)
    return packages


def _walk_v1(block: dict[str, Any], into: dict[str, Package]) -> None:
    """v1 nests transitive packages inside their parents; flatten by name."""
    for name, entry in block.items():
        if not isinstance(entry, dict):
            continue
        resolved = str(entry.get("resolved") or "")
        into.setdefault(
            str(name),
            Package(
                version=str(entry.get("version") or ""),
                source=resolved,
                integrity=str(entry.get("integrity") or ""),
                ref=_git_ref(resolved),
            ),
        )
        nested = entry.get("dependencies")
        if isinstance(nested, dict):
            _walk_v1(nested, into)


def _parse_pnpm(text: str) -> dict[str, Package]:
    """``pnpm-lock.yaml``: keys are ``/name@version`` or ``/name/version``."""
    data = yaml.safe_load(text)
    if not isinstance(data, dict):
        return {}
    packages: dict[str, Package] = {}
    entries = data.get("packages")
    if isinstance(entries, dict):
        for key, entry in entries.items():
            name, version = _split_pnpm_key(str(key))
            if not name:
                continue
            resolution = entry.get("resolution") if isinstance(entry, dict) else None
            integrity = ""
            source = ""
            if isinstance(resolution, dict):
                integrity = str(resolution.get("integrity") or "")
                source = str(resolution.get("tarball") or resolution.get("repo") or "")
            packages[name] = Package(version=version, source=source, integrity=integrity)
    for section in ("dependencies", "devDependencies"):
        block = data.get(section)
        if isinstance(block, dict):
            for name in block:
                existing = packages.get(str(name), Package())
                packages[str(name)] = Package(
                    version=existing.version,
                    source=existing.source,
                    integrity=existing.integrity,
                    direct=True,
                    ref=existing.ref,
                )
    return packages


def _split_pnpm_key(key: str) -> tuple[str, str]:
    """``/@scope/name@1.2.3`` or ``/name/1.2.3`` -> ``("@scope/name", "1.2.3")``."""
    body = key.lstrip("/")
    if "@" in body[1:]:
        name, _, version = body.rpartition("@")
        if name:
            return name, version.split("(")[0]
    name, _, version = body.rpartition("/")
    return (name, version) if name else (body, "")


def _parse_yarn_berry(text: str) -> dict[str, Package]:
    """Yarn Berry lockfiles are YAML with ``name@range`` keys and a ``checksum``."""
    data = yaml.safe_load(text)
    if not isinstance(data, dict):
        return {}
    packages: dict[str, Package] = {}
    for key, entry in data.items():
        if key == "__metadata" or not isinstance(entry, dict):
            continue
        name = _name_from_descriptor(str(key).split(",")[0].strip().strip('"'))
        if not name:
            continue
        resolution = str(entry.get("resolution") or "")
        packages[name] = Package(
            version=str(entry.get("version") or ""),
            source=resolution if "://" in resolution or "@git" in resolution else "",
            integrity=str(entry.get("checksum") or ""),
            ref=_git_ref(resolution),
        )
    return packages


_CLASSIC_HEADER = re.compile(r'^"?([^\s,"]+)"?(?:,.*)?:\s*$')
_CLASSIC_FIELD = re.compile(r'^\s{2,}(version|resolved|integrity)\s+"?([^"]*)"?\s*$')


def _parse_yarn_classic(text: str) -> dict[str, Package]:
    """Yarn Classic: ``name@range:`` headers with two-space-indented fields."""
    packages: dict[str, Package] = {}
    name = ""
    fields: dict[str, str] = {}

    def flush() -> None:
        if not name:
            return
        resolved = fields.get("resolved", "")
        packages[name] = Package(
            version=fields.get("version", ""),
            source=resolved if "://" in resolved else "",
            integrity=fields.get("integrity", ""),
            ref=_git_ref(resolved),
        )

    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line[0].isspace():
            flush()
            header = _CLASSIC_HEADER.match(line.strip())
            name = _name_from_descriptor(header.group(1)) if header else ""
            fields = {}
            continue
        if field := _CLASSIC_FIELD.match(line):
            fields[field.group(1)] = field.group(2)
    flush()
    return packages


def _name_from_descriptor(descriptor: str) -> str:
    """``@scope/pkg@^1.0.0`` -> ``@scope/pkg``; ``pkg@npm:1.0.0`` -> ``pkg``."""
    body = descriptor.strip().strip('"')
    if not body:
        return ""
    scope, rest = ("@", body[1:]) if body.startswith("@") else ("", body)
    return scope + rest.split("@", 1)[0]


def _git_ref(spec: str) -> str:
    """The git reference a spec pins to, when it resolves from a repository.

    Only ever populated for a git-sourced package. A registry tarball has a
    version, not a reference, and calling its URL fragment a reference would make
    every ordinary dependency look mutable.
    """
    if not spec or not (_GIT_SPEC.match(spec) or ".git" in spec):
        return ""
    return spec.rpartition("#")[2] if "#" in spec else ""


NPM = Ecosystem(
    name="npm",
    manifests=("package.json",),
    locks=("package-lock.json", "npm-shrinkwrap.json", "yarn.lock", "pnpm-lock.yaml"),
    parse_manifest=parse_manifest,
    parse_lock=parse_lock,
)
