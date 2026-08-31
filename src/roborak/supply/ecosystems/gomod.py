"""Go modules.

``go.mod`` is a small line-based grammar and ``go.sum`` is two or three columns
per line. Neither needs a real parser, and both carry the two facts this stage is
about: which module versions are required, and whether each still has a hash.
"""

from __future__ import annotations

import re

from roborak.supply.ecosystems.base import Ecosystem, Package, exact_version_satisfies

_REQUIRE_LINE = re.compile(r"^\s*(?P<path>[^\s()]+)\s+(?P<version>v\S+)")
_SINGLE_REQUIRE = re.compile(r"^\s*require\s+(?P<path>\S+)\s+(?P<version>v\S+)")
_REPLACE = re.compile(r"^\s*replace\s+(?P<from>\S+)(?:\s+\S+)?\s*=>\s*(?P<to>\S+)")
_REPLACE_ENTRY = re.compile(r"^\s*(?P<from>[^\s()]+)(?:\s+\S+)?\s*=>\s*(?P<to>\S+)")
_INDIRECT = re.compile(r"//.*\bindirect\b")


def parse_manifest(text: str) -> dict[str, Package]:
    """``go.mod``: ``require`` blocks and single-line requires, plus ``replace``.

    ``replace`` matters more here than the version does. It redirects a module
    path to a different repository or a local directory, which is a source change
    that no version number reflects.

    Both ``require`` and ``replace`` come in single-line and parenthesised block
    forms, and go's own tooling writes the block form once there is more than one
    entry -- so a parser that reads only the single-line form sees nothing in a
    typical file.
    """
    packages: dict[str, Package] = {}
    in_require = False
    in_replace = False
    for raw in text.splitlines():
        # ``// indirect`` is a comment carrying the one fact that separates a
        # declared dependency from an inherited one, so it is read before
        # comments are stripped.
        direct = not _INDIRECT.search(raw)
        line = raw.split("//", 1)[0].rstrip()
        if not line.strip():
            continue
        if line.strip().startswith("require ("):
            in_require = True
            continue
        if line.strip().startswith("replace ("):
            in_replace = True
            continue
        if (in_require or in_replace) and line.strip() == ")":
            in_require = in_replace = False
            continue
        if in_require:
            if match := _REQUIRE_LINE.match(line):
                _record_require(packages, match.group("path"), match.group("version"), direct)
            continue
        if in_replace:
            if entry := _REPLACE_ENTRY.match(line):
                _apply_replace(packages, entry.group("from"), entry.group("to"))
            continue
        if match := _SINGLE_REQUIRE.match(line):
            _record_require(packages, match.group("path"), match.group("version"), direct)
            continue
        if replaced := _REPLACE.match(line):
            _apply_replace(packages, replaced.group("from"), replaced.group("to"))
    return packages


def _record_require(packages: dict[str, Package], path: str, version: str, direct: bool) -> None:
    """Record a required module without discarding a replacement already seen.

    ``replace`` may be written above ``require`` -- go does not fix the order --
    so the require line updates the entry rather than replacing it, keeping the
    redirect ``_apply_replace`` recorded.
    """
    existing = packages.get(path)
    packages[path] = Package(
        version=version,
        source=existing.source if existing is not None else "",
        direct=direct,
    )


def _apply_replace(packages: dict[str, Package], path: str, target: str) -> None:
    """Point an already-required module at its replacement source."""
    existing = packages.get(path, Package(direct=True))
    packages[path] = Package(
        version=existing.version,
        source=target,
        direct=existing.direct,
    )


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
    version_satisfies=exact_version_satisfies,
)
