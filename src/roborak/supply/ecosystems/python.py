"""Python: pyproject/requirements manifests, uv and poetry locks.

TOML is read with the standard library's ``tomllib`` -- Python 3.11 added it and
roborak requires 3.12, so supporting two lockfile formats and one manifest costs
no new dependency.
"""

from __future__ import annotations

import re
import tomllib

from roborak.supply.ecosystems.base import Ecosystem, Package

_REQUIREMENT = re.compile(
    r"^\s*(?P<name>[A-Za-z0-9._-]+)\s*(?P<spec>[<>=!~]=?[^;#\s]*)?",
)
_URL_REQUIREMENT = re.compile(r"^\s*(?P<name>[A-Za-z0-9._-]+)\s*@\s*(?P<url>\S+)")


def parse_manifest(text: str) -> dict[str, Package]:
    """``pyproject.toml`` or a ``requirements*.txt``, sniffed by content."""
    stripped = text.lstrip()
    if stripped.startswith("[") or "\n[" in text:
        parsed = _parse_pyproject(text)
        if parsed:
            return parsed
    return _parse_requirements(text)


def _parse_pyproject(text: str) -> dict[str, Package]:
    data = tomllib.loads(text)
    packages: dict[str, Package] = {}

    project = data.get("project")
    if isinstance(project, dict):
        _add_requirement_list(project.get("dependencies"), packages)
        optional = project.get("optional-dependencies")
        if isinstance(optional, dict):
            for group in optional.values():
                _add_requirement_list(group, packages)

    groups = data.get("dependency-groups")
    if isinstance(groups, dict):
        for group in groups.values():
            _add_requirement_list(group, packages)

    tool = data.get("tool")
    if isinstance(tool, dict):
        poetry = tool.get("poetry")
        if isinstance(poetry, dict):
            _add_poetry_block(poetry.get("dependencies"), packages)
            dev = poetry.get("dev-dependencies")
            _add_poetry_block(dev, packages)
    return packages


def _add_requirement_list(entries: object, into: dict[str, Package]) -> None:
    """PEP 508 requirement strings, as ``project.dependencies`` holds them."""
    if not isinstance(entries, list):
        return
    for entry in entries:
        if not isinstance(entry, str):
            continue
        if url := _URL_REQUIREMENT.match(entry):
            into[_normalise(url.group("name"))] = Package(
                source=url.group("url"), direct=True, ref=_git_ref(url.group("url"))
            )
            continue
        if match := _REQUIREMENT.match(entry):
            into[_normalise(match.group("name"))] = Package(
                version=(match.group("spec") or "").strip(), direct=True
            )


def _add_poetry_block(block: object, into: dict[str, Package]) -> None:
    """Poetry's table form, where a value may be a string or a table."""
    if not isinstance(block, dict):
        return
    for name, spec in block.items():
        if name == "python":
            continue
        if isinstance(spec, dict):
            source = str(spec.get("git") or spec.get("url") or spec.get("path") or "")
            ref = str(spec.get("rev") or spec.get("branch") or spec.get("tag") or "")
            into[_normalise(str(name))] = Package(
                version=str(spec.get("version") or ""), source=source, direct=True, ref=ref
            )
        else:
            into[_normalise(str(name))] = Package(version=str(spec), direct=True)


def _parse_requirements(text: str) -> dict[str, Package]:
    """``requirements.txt``: one requirement per line, ``#`` comments, ``-r`` includes."""
    packages: dict[str, Package] = {}
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        if url := _URL_REQUIREMENT.match(line):
            packages[_normalise(url.group("name"))] = Package(
                source=url.group("url"), direct=True, ref=_git_ref(url.group("url"))
            )
            continue
        if match := _REQUIREMENT.match(line):
            packages[_normalise(match.group("name"))] = Package(
                version=(match.group("spec") or "").strip(), direct=True
            )
    return packages


def parse_lock(text: str) -> dict[str, Package]:
    """``uv.lock`` or ``poetry.lock``; both are TOML with a ``[[package]]`` array."""
    data = tomllib.loads(text)
    entries = data.get("package")
    if not isinstance(entries, list):
        return {}

    packages: dict[str, Package] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = _normalise(str(entry.get("name") or ""))
        if not name:
            continue
        source, ref = _source_of(entry)
        packages[name] = Package(
            version=str(entry.get("version") or ""),
            source=source,
            integrity=_integrity_of(entry),
            ref=ref,
        )
    return packages


def _source_of(entry: dict[str, object]) -> tuple[str, str]:
    """Where a locked package resolves from, and any git reference it pins."""
    source = entry.get("source")
    if isinstance(source, dict):
        # uv: {registry = "..."} | {git = "...?rev=..."} | {url = ...} | {path = ...}
        for key in ("registry", "git", "url", "path", "directory", "editable"):
            if value := source.get(key):
                url = str(value)
                return url, _git_ref(url) if key == "git" else ""
        if reference := source.get("reference"):
            # poetry: {type = "git", url = "...", reference = "main"}
            return str(source.get("url") or ""), str(reference)
    return "", ""


def _integrity_of(entry: dict[str, object]) -> str:
    """The artefact hash, whichever of the two shapes the resolver used.

    uv records ``[package.wheels]`` / ``sdist`` tables carrying a ``hash``; poetry
    records a ``files`` array of ``{file, hash}``. Absence is the interesting case
    either way -- a locked package with no hash is one nothing verifies.
    """
    sdist = entry.get("sdist")
    if isinstance(sdist, dict) and (value := sdist.get("hash")):
        return str(value)
    wheels = entry.get("wheels")
    if isinstance(wheels, list):
        for wheel in wheels:
            if isinstance(wheel, dict) and (value := wheel.get("hash")):
                return str(value)
    files = entry.get("files")
    if isinstance(files, list):
        for record in files:
            if isinstance(record, dict) and (value := record.get("hash")):
                return str(value)
    return ""


def _git_ref(url: str) -> str:
    """The ``rev``/``ref``/``branch`` a git URL pins, or its fragment."""
    if "git" not in url.lower():
        return ""
    query = url.rpartition("?")[2] if "?" in url else ""
    for key in ("rev=", "ref=", "tag=", "branch="):
        if key in query:
            return query.split(key, 1)[1].split("&")[0]
    return url.rpartition("#")[2] if "#" in url else ""


def _normalise(name: str) -> str:
    """PEP 503 normalisation, so ``Foo.Bar`` and ``foo-bar`` are one package."""
    return re.sub(r"[-_.]+", "-", name).lower()


PYTHON = Ecosystem(
    name="python",
    manifests=("pyproject.toml", "setup.cfg"),
    # Pipfile.lock is deliberately absent: it is JSON, not TOML, so claiming it
    # would report an ecosystem as analysed while parsing nothing out of it.
    locks=("uv.lock", "poetry.lock", "pdm.lock"),
    parse_manifest=parse_manifest,
    parse_lock=parse_lock,
    manifest_prefixes=("requirements",),
)
