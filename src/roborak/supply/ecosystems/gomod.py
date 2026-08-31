"""Go modules.

``go.mod`` is a small line-based grammar and ``go.sum`` is two or three columns
per line. Neither needs a real parser, and both carry the two facts this stage is
about: which module versions are required, and whether each still has a hash.
"""

from __future__ import annotations

import re

from roborak.supply.ecosystems.base import Ecosystem, Package

_REQUIRE_LINE = re.compile(r"^\s*(?P<path>[^\s()]+)\s+(?P<version>v\S+)")
_SINGLE_REQUIRE = re.compile(r"^\s*require\s+(?P<path>\S+)\s+(?P<version>v\S+)")
_REPLACE = re.compile(r"^\s*replace\s+(?P<from>\S+)(?:\s+\S+)?\s*=>\s*(?P<to>\S+)")


def parse_manifest(text: str) -> dict[str, Package]:
    """``go.mod``: ``require`` blocks and single-line requires, plus ``replace``.

    ``replace`` matters more here than the version does. It redirects a module
    path to a different repository or a local directory, which is a source change
    that no version number reflects.
    """
    packages: dict[str, Package] = {}
    in_require = False
    for raw in text.splitlines():
        line = raw.split("//", 1)[0].rstrip()
        if not line.strip():
            continue
        if line.strip().startswith("require ("):
            in_require = True
            continue
        if in_require and line.strip() == ")":
            in_require = False
            continue
        if in_require:
            if match := _REQUIRE_LINE.match(line):
                packages[match.group("path")] = Package(version=match.group("version"), direct=True)
            continue
        if match := _SINGLE_REQUIRE.match(line):
            packages[match.group("path")] = Package(version=match.group("version"), direct=True)
            continue
        if replaced := _REPLACE.match(line):
            path = replaced.group("from")
            existing = packages.get(path, Package(direct=True))
            packages[path] = Package(
                version=existing.version,
                source=replaced.group("to"),
                direct=True,
            )
    return packages


def parse_lock(text: str) -> dict[str, Package]:
    """``go.sum``: ``<module> <version>[/go.mod] <h1:hash>``.

    The ``/go.mod`` rows hash the manifest rather than the module zip, so a module
    that keeps only those has lost the hash that covers its actual contents.
    """
    packages: dict[str, Package] = {}
    for raw in text.splitlines():
        parts = raw.split()
        if len(parts) < 3:
            continue
        path, version, digest = parts[0], parts[1], parts[2]
        if version.endswith("/go.mod"):
            version = version[: -len("/go.mod")]
            if path in packages:
                continue
            packages[path] = Package(version=version)
            continue
        packages[path] = Package(version=version, integrity=digest)
    return packages


GO = Ecosystem(
    name="go",
    manifests=("go.mod",),
    locks=("go.sum",),
    parse_manifest=parse_manifest,
    parse_lock=parse_lock,
)
