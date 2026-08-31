"""Turn two parses of the same file into facts about what moved.

The ordering rule throughout: a change to *where* a package comes from, or to
whether anything verifies it, outranks a change to which version it is. A version
bump is routine and a registry swap is not, so when one package moves in several
ways at once the report names the one a reviewer would want to see first.
"""

from __future__ import annotations

from roborak.core.models import DependencyChange, DependencyChangeKind
from roborak.supply.ecosystems.base import Package, classify_version_move


def compare(
    ecosystem: str, old: dict[str, Package], new: dict[str, Package]
) -> list[DependencyChange]:
    """Every movement between two parses of one file, most alarming first."""
    changes: list[DependencyChange] = []

    for name in sorted(set(new) - set(old)):
        package = new[name]
        changes.append(
            DependencyChange(
                ecosystem=ecosystem,
                name=name,
                kind=DependencyChangeKind.ADDED,
                new_version=package.version,
                new_source=package.source,
                direct=package.direct,
                note=_note_for_new(package),
            )
        )

    for name in sorted(set(old) - set(new)):
        package = old[name]
        changes.append(
            DependencyChange(
                ecosystem=ecosystem,
                name=name,
                kind=DependencyChangeKind.REMOVED,
                old_version=package.version,
                old_source=package.source,
                direct=package.direct,
            )
        )

    for name in sorted(set(old) & set(new)):
        if (change := _compare_one(ecosystem, name, old[name], new[name])) is not None:
            changes.append(change)

    return sorted(changes, key=_rank)


def _compare_one(ecosystem: str, name: str, old: Package, new: Package) -> DependencyChange | None:
    """How one package moved, or ``None`` when it did not."""

    def moved(kind: DependencyChangeKind, note: str = "") -> DependencyChange:
        return DependencyChange(
            ecosystem=ecosystem,
            name=name,
            kind=kind,
            old_version=old.version,
            new_version=new.version,
            old_source=old.source,
            new_source=new.source,
            direct=new.direct or old.direct,
            note=note,
        )

    if old.source != new.source:
        return moved(DependencyChangeKind.SOURCE_CHANGED, _note_for_source(old, new))
    if old.integrity and not new.integrity:
        return moved(
            DependencyChangeKind.INTEGRITY_LOST,
            "It had a checksum and no longer does, so nothing verifies what gets installed.",
        )
    if old.integrity and new.integrity and old.integrity != new.integrity:
        # Same name and same version with a different hash means the artefact
        # behind that version was replaced, which no version number can express.
        return moved(
            DependencyChangeKind.INTEGRITY_CHANGED,
            "The published artefact changed without the version changing."
            if old.version == new.version
            else "",
        )
    if old.ref != new.ref and (old.ref or new.ref):
        return moved(DependencyChangeKind.SOURCE_CHANGED, _note_for_ref(new))
    if old.version != new.version:
        return moved(classify_version_move(old.version, new.version))
    return None


def drift(
    ecosystem: str, manifest: dict[str, Package], lock: dict[str, Package]
) -> list[DependencyChange]:
    """Where the manifest and the lockfile disagree about the same change.

    Only the two directions that mean the resolved tree is not the declared one:
    a package the manifest asks for that the lock has never heard of, and one the
    lock still pins that the manifest no longer wants. Version-range mismatches
    are not drift -- a range is supposed to be wider than the pin that satisfies
    it -- so they are deliberately not reported here.
    """
    changes: list[DependencyChange] = []
    for name in sorted(set(manifest) - set(lock)):
        changes.append(
            DependencyChange(
                ecosystem=ecosystem,
                name=name,
                kind=DependencyChangeKind.MANIFEST_LOCK_DRIFT,
                new_version=manifest[name].version,
                direct=True,
                note=(
                    "The manifest requires this and the lockfile does not resolve it, so an "
                    "install would produce a tree nobody reviewed."
                ),
            )
        )
    for name in sorted(set(lock) - set(manifest)):
        package = lock[name]
        if not package.direct:
            # A transitive package is in the lock and not the manifest by design.
            continue
        changes.append(
            DependencyChange(
                ecosystem=ecosystem,
                name=name,
                kind=DependencyChangeKind.MANIFEST_LOCK_DRIFT,
                old_version=package.version,
                direct=True,
                note="The lockfile still pins this and the manifest no longer declares it.",
            )
        )
    return changes


def _note_for_new(package: Package) -> str:
    """Why a newly added package might deserve a second look."""
    reasons: list[str] = []
    if not package.direct:
        reasons.append("pulled in transitively rather than declared")
    if package.mutable_ref:
        reasons.append(f"pinned to the mutable reference `{package.ref}`")
    if package.source and not package.integrity:
        reasons.append("resolved from a source with no checksum recorded")
    return "; ".join(reasons).capitalize() + "." if reasons else ""


def _note_for_source(old: Package, new: Package) -> str:
    old_source = old.source or "the default registry"
    new_source = new.source or "the default registry"
    return f"Now resolved from {new_source} instead of {old_source}."


def _note_for_ref(new: Package) -> str:
    if new.mutable_ref:
        return (
            f"Pinned to `{new.ref}`, which is a moving reference: what installs later need "
            "not be what was reviewed."
        )
    return f"Now pinned to `{new.ref}`." if new.ref else "No longer pinned to a git reference."


_RANK: dict[DependencyChangeKind, int] = {
    DependencyChangeKind.SOURCE_CHANGED: 0,
    DependencyChangeKind.INTEGRITY_LOST: 1,
    DependencyChangeKind.INTEGRITY_CHANGED: 2,
    DependencyChangeKind.MANIFEST_LOCK_DRIFT: 3,
    DependencyChangeKind.ADDED: 4,
    DependencyChangeKind.DOWNGRADED: 5,
    DependencyChangeKind.REMOVED: 6,
    DependencyChangeKind.UPGRADED: 7,
}


def _rank(change: DependencyChange) -> tuple[int, int, str]:
    """Sort key: kind first, then direct dependencies, then name for determinism.

    This ordering is load-bearing rather than cosmetic. ``max_changes`` truncates
    the tail, so whatever sorts last is what a large lockfile regeneration drops --
    and the right thing to drop is the nine hundredth routine version bump.
    """
    return (_RANK.get(change.kind, 9), 0 if change.direct else 1, change.name)
