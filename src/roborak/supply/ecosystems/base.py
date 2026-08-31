"""The contract an ecosystem parser implements."""

from __future__ import annotations

import posixpath
from collections.abc import Callable
from dataclasses import dataclass, field

from roborak.core.models import DependencyChangeKind

MUTABLE_GIT_REFS = frozenset({"main", "master", "head", "develop", "trunk", "latest", ""})
"""Git references that name a moving target rather than a commit.

A dependency pinned to one of these installs whatever that branch holds at install
time, which is not what anybody reviewed. A 40-character hex sha is the only
reference that cannot change under the project."""


@dataclass(frozen=True)
class Package:
    """One resolved package, as much as its ecosystem records about it."""

    version: str = ""
    source: str = ""
    """Registry URL, git remote, or local path. Empty means the ecosystem's
    default registry, which is itself the fact a source change moves away from."""

    integrity: str = ""
    """Checksum, hash or digest. Empty means nothing verifies what is installed."""

    direct: bool = False
    """Named by a manifest rather than pulled in by another package."""

    ref: str = ""
    """The git reference, for a package resolved from a repository rather than a
    registry. Kept separately from ``version`` because a branch name and a version
    number are not the same claim."""

    @property
    def mutable_ref(self) -> bool:
        """Whether this resolves to something that can change without the diff moving."""
        if not self.ref:
            return False
        return not (len(self.ref) == 40 and all(c in "0123456789abcdef" for c in self.ref.lower()))


Parser = Callable[[str], dict[str, Package]]


@dataclass(frozen=True)
class Ecosystem:
    """One package manager: the files it owns and how to read them."""

    name: str
    manifests: tuple[str, ...]
    """Basenames of human-authored manifests, e.g. ``package.json``."""

    locks: tuple[str, ...]
    """Basenames of resolver output, e.g. ``package-lock.json``."""

    parse_manifest: Parser
    parse_lock: Parser
    manifest_prefixes: tuple[str, ...] = field(default=())
    """Basename prefixes that are also manifests, for the ecosystems that allow a
    family of them (``requirements-dev.txt`` next to ``requirements.txt``)."""

    def is_manifest(self, path: str) -> bool:
        base = posixpath.basename(path)
        if base in self.manifests:
            return True
        return any(
            base.startswith(prefix) and base.endswith(".txt") for prefix in self.manifest_prefixes
        )

    def is_lock(self, path: str) -> bool:
        return posixpath.basename(path) in self.locks

    def handles(self, path: str) -> bool:
        return self.is_manifest(path) or self.is_lock(path)

    def read(self, path: str, text: str) -> dict[str, Package]:
        """Parse ``text`` as whichever of the two kinds ``path`` is.

        Total by contract: a parser that raises on a file the ecosystem claimed is
        a bug in the parser, and the caller still gets an empty result rather than
        a failed review.
        """
        parser = self.parse_lock if self.is_lock(path) else self.parse_manifest
        try:
            return parser(text)
        except Exception:  # noqa: BLE001 - a resolver's format is not our invariant
            return {}


def classify_version_move(old: str, new: str) -> DependencyChangeKind:
    """Whether a version string moved forwards or backwards.

    Compared component by component on the leading digits, which is enough for the
    question being asked -- a reviewer wants to know that something went *down*,
    and the exact semantics of a build suffix do not change that. Anything that
    does not compare numerically is reported as an upgrade rather than guessed at.
    """
    for old_part, new_part in zip(_numeric_parts(old), _numeric_parts(new), strict=False):
        if old_part == new_part:
            continue
        return (
            DependencyChangeKind.DOWNGRADED
            if new_part < old_part
            else DependencyChangeKind.UPGRADED
        )
    return DependencyChangeKind.UPGRADED


def _numeric_parts(version: str) -> list[int]:
    """The leading numeric components of a version, e.g. ``1.20.3-rc1`` -> ``[1, 20, 3]``."""
    parts: list[int] = []
    for chunk in version.lstrip("v=^~> ").split("."):
        digits = ""
        for char in chunk:
            if not char.isdigit():
                break
            digits += char
        if not digits:
            break
        parts.append(int(digits))
    return parts
