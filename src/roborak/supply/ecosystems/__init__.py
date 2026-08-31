"""One parser per ecosystem, and the dispatch that picks between them.

Every parser obeys the same three rules:

- **Total.** A malformed, truncated or unexpected-version file yields ``{}``, never
  an exception. A lockfile arrives from whatever wrote it, and a review that fell
  over because a resolver changed its output format would be worse than a review
  that quietly reported the ecosystem as unreadable.
- **Whole-file.** Parsers read a complete file, never a diff hunk. Both sides are
  parsed and the results compared, so a reformat that moves every line produces no
  changes at all -- which is the correct answer.
- **Name-keyed.** The unit is a package name, because that is what a delta is
  about. A resolver's own node ids differ between versions of the resolver.
"""

from __future__ import annotations

from roborak.core.models import AssetKind
from roborak.supply.ecosystems.base import Ecosystem, Package
from roborak.supply.ecosystems.cargo import CARGO
from roborak.supply.ecosystems.composer import COMPOSER
from roborak.supply.ecosystems.gomod import GO
from roborak.supply.ecosystems.npm import NPM
from roborak.supply.ecosystems.python import PYTHON

ECOSYSTEMS: list[Ecosystem] = [NPM, PYTHON, GO, CARGO, COMPOSER]

__all__ = [
    "ECOSYSTEMS",
    "Ecosystem",
    "Package",
    "ecosystem_for",
    "kind_for_dependency_file",
]


def ecosystem_for(path: str) -> Ecosystem | None:
    """The ecosystem that can read ``path``, or ``None`` if none can."""
    return next((eco for eco in ECOSYSTEMS if eco.handles(path)), None)


def kind_for_dependency_file(path: str) -> AssetKind | None:
    """Whether ``path`` is a manifest, a lockfile, or neither.

    Only files a parser here can actually read are claimed. An ecosystem roborak
    does not support is left unclassified rather than reported as a dependency
    change nobody analysed.
    """
    for eco in ECOSYSTEMS:
        if eco.is_lock(path):
            return AssetKind.DEPENDENCY_LOCK
        if eco.is_manifest(path):
            return AssetKind.DEPENDENCY_MANIFEST
    return None
