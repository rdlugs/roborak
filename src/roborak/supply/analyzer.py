"""The stage: classify what changed, read both sides, report what moved.

Non-fatal by construction, the way the blast-radius pass is. Every failure in here
-- an unreadable revision, an ecosystem with no parser, a lockfile too big to
bother with -- becomes a status and a note rather than an exception, because a
review that fell over because it could not parse a lockfile would be worse than
one that says so.
"""

from __future__ import annotations

import logging
from pathlib import Path

from roborak.core.config import SupplyChainConfig
from roborak.core.models import (
    AssetKind,
    ChangedAsset,
    ChangeSet,
    DependencyChange,
    Finding,
    SupplyChainReport,
    SupplyChainStatus,
)
from roborak.supply.classify import classify
from roborak.supply.delta import compare, drift, sort_changes
from roborak.supply.ecosystems import ECOSYSTEMS, ecosystem_for
from roborak.supply.ecosystems.base import Ecosystem, Package
from roborak.supply.revision import base_revision, read_at, read_working_tree

log = logging.getLogger(__name__)

Pair = tuple[str, str]
"""An ecosystem name and the directory its manifest/lock pair lives in.

A repository holds one dependency tree per directory that has a manifest, not one
per ecosystem, so the directory is part of the identity of a pair. Without it a
monorepo's apps get compared against each other's lockfiles and every app's
dependencies look like drift."""

CHECKED_OUT_ORIGINS = {"local", "paths"}
"""Origins whose files can be read from git. A forge diff is a patch we fetched;
the lockfile bodies it needs are not on disk, and reading the local checkout's
copies would describe a different change entirely."""

SCANNERS = frozenset({"actionlint", "hadolint", "checkov", "osv-scanner"})
"""Static adapters that belong to this stage rather than to the language pass.
When one of these is skipped, the reason belongs in this report's notes, where a
reader is already looking for what was not checked."""

_KNOWN_LOCK_NAMES = frozenset(
    {
        "Pipfile.lock",
        "Gemfile.lock",
        "packages.lock.json",
        "gradle.lockfile",
        "mix.lock",
        "pubspec.lock",
        "flake.lock",
    }
)
"""Lockfiles roborak recognises by name and cannot parse. Naming them explicitly
is the difference between "we support that and found nothing" and "nobody looked",
and the second is what a reader needs to be told."""


def analyse(
    changeset: ChangeSet,
    repo: Path,
    config: SupplyChainConfig,
) -> SupplyChainReport | None:
    """What this change does to dependencies and infrastructure, or ``None``.

    ``None`` means the stage was switched off, and nothing else does. Every other
    outcome -- including "there was nothing here to look at" -- is a report with a
    status, because a reader cannot otherwise tell a change that touches no
    dependency from a review that never checked.
    """
    if not config.enabled:
        return None

    report = SupplyChainReport()
    assets, unsupported = _assets(changeset, config, report)
    report.assets = assets
    for note in unsupported:
        report.notes.append(note)

    if not assets and not unsupported:
        report.status = SupplyChainStatus.NOTHING_RELEVANT
        return report

    if not assets:
        report.status = SupplyChainStatus.UNSUPPORTED
        return report

    report.status = SupplyChainStatus.ANALYSED
    dependency_assets = [
        asset
        for asset in assets
        if asset.kind in {AssetKind.DEPENDENCY_MANIFEST, AssetKind.DEPENDENCY_LOCK}
    ]
    if not dependency_assets:
        # Infrastructure changed but no dependency did. There is no delta to
        # compute, and the prompt gating still has the asset kinds it needs.
        return report

    if changeset.origin not in CHECKED_OUT_ORIGINS:
        report.status = SupplyChainStatus.UNAVAILABLE
        report.notes.append(
            f"Dependency files changed, but {changeset.origin} changes are not checked out, so "
            "neither side could be read. No dependency delta was computed."
        )
        return report

    _analyse_dependencies(dependency_assets, changeset, repo, config, report)
    return report


def _assets(
    changeset: ChangeSet, config: SupplyChainConfig, report: SupplyChainReport
) -> tuple[list[ChangedAsset], list[str]]:
    """The classified assets this change touches, and notes for what it cannot read.

    Reads ``changeset.files`` before ``ignore_paths`` has been applied -- the whole
    point of running this stage from the CLI rather than from inside ``Reviewer``.
    """
    assets: list[ChangedAsset] = []
    unsupported: list[str] = []
    seen_unsupported: set[str] = set()

    for file in changeset.files:
        if file.is_binary:
            continue
        path = file.path
        if (kind := classify(path)) is not None:
            assets.append(ChangedAsset(path=path, kind=kind))
            continue
        name = path.rsplit("/", 1)[-1]
        if name in _KNOWN_LOCK_NAMES and name not in seen_unsupported:
            seen_unsupported.add(name)
            unsupported.append(
                f"`{path}` changed, but roborak has no parser for {name}; its dependency "
                "changes were not analysed."
            )

    if len(assets) > config.max_assets:
        report.truncated = True
        report.notes.append(
            f"{len(assets)} dependency and infrastructure files changed; the first "
            f"{config.max_assets} were analysed (`supply_chain.max_assets`)."
        )
        assets = assets[: config.max_assets]
    return assets, unsupported


def _analyse_dependencies(
    assets: list[ChangedAsset],
    changeset: ChangeSet,
    repo: Path,
    config: SupplyChainConfig,
    report: SupplyChainReport,
) -> None:
    """Read both sides of every dependency file and reduce them to a delta."""
    base = base_revision(
        repo, changeset.base_sha or changeset.base_ref or "", timeout=config.timeout_seconds
    )
    head = changeset.head_sha or changeset.head_ref or ""

    changes: list[DependencyChange] = []
    ecosystems: list[str] = []
    ecosystem_defs: dict[str, Ecosystem] = {}
    # Head-side parses keyed by ecosystem *and directory*, so drift can compare a
    # manifest against the lockfile that is supposed to satisfy it even though
    # they are separate files. The directory is half the key because a monorepo
    # has many `package.json` files that answer to different lockfiles, and
    # merging them would report every app's dependencies as missing from every
    # other app's lock.
    manifests: dict[Pair, dict[str, Package]] = {}
    locks: dict[Pair, dict[str, Package]] = {}

    for asset in assets:
        eco = ecosystem_for(asset.path)
        if eco is None:
            continue
        old_text = read_at(repo, base, asset.path, timeout=config.timeout_seconds)
        new_text = read_working_tree(repo, asset.path)
        changed_file = changeset.file_by_path(asset.path)
        deleted = changed_file is not None and changed_file.change_type == "deleted"
        if new_text is None and head and not deleted:
            new_text = read_at(repo, head, asset.path, timeout=config.timeout_seconds)

        if old_text is None and new_text is None:
            report.notes.append(
                f"Neither side of `{asset.path}` could be read from git, so it was not analysed."
            )
            continue

        old = eco.read(asset.path, old_text) if old_text else {}
        new = eco.read(asset.path, new_text) if new_text else {}
        if not old and not new:
            report.notes.append(
                f"`{asset.path}` could not be parsed as {eco.name}; its changes were not analysed."
            )
            continue

        if eco.name not in ecosystems:
            ecosystems.append(eco.name)
        ecosystem_defs[eco.name] = eco
        changes.extend(compare(eco.name, old, new))
        target = manifests if asset.kind is AssetKind.DEPENDENCY_MANIFEST else locks
        target.setdefault(_pair_key(eco, asset.path), {}).update(new)

    _fill_counterparts(assets, repo, manifests, locks, report)
    for key in sorted(set(manifests) & set(locks)):
        changes.extend(drift(ecosystem_defs[key[0]], manifests[key], locks[key]))

    report.ecosystems = ecosystems
    changes = sort_changes(changes)
    if len(changes) > config.max_changes:
        report.truncated = True
        report.notes.append(
            f"{len(changes)} dependency changes were found; the {config.max_changes} most "
            "significant are listed (`supply_chain.max_changes`). Source, integrity and drift "
            "changes are never truncated away before ordinary version bumps."
        )
        changes = changes[: config.max_changes]
    report.changes = changes

    unread = {asset.path for asset in assets} - {
        asset.path for asset in assets if ecosystem_for(asset.path) is not None
    }
    if unread:
        log.debug("no ecosystem parser for %s", ", ".join(sorted(unread)))


def _pair_key(eco: Ecosystem, path: str) -> Pair:
    """Which manifest/lock pair ``path`` belongs to."""
    return (eco.name, path.rpartition("/")[0])


def _fill_counterparts(
    assets: list[ChangedAsset],
    repo: Path,
    manifests: dict[Pair, dict[str, Package]],
    locks: dict[Pair, dict[str, Package]],
    report: SupplyChainReport,
) -> None:
    """Read the side of a pair the change did not touch.

    Drift is only visible when both halves are in hand, and the case worth
    catching is exactly the one where only half changed: a manifest edited without
    re-running the resolver, or a lockfile regenerated against a manifest that
    still says something else. Reading only the changed files would make the most
    interesting outcome the one that cannot be detected.

    Only the head side is read, and only from the working tree, because drift is a
    statement about the state this change leaves behind rather than about a
    movement between two revisions.
    """
    for asset in assets:
        eco = ecosystem_for(asset.path)
        if eco is None:
            continue
        wanted = locks if asset.kind is AssetKind.DEPENDENCY_MANIFEST else manifests
        key = _pair_key(eco, asset.path)
        if key in wanted:
            continue
        names = eco.locks if asset.kind is AssetKind.DEPENDENCY_MANIFEST else eco.manifests
        directory = key[1]
        for name in names:
            candidate = f"{directory}/{name}" if directory else name
            text = read_working_tree(repo, candidate)
            if text is None:
                continue
            parsed = eco.read(candidate, text)
            if parsed:
                wanted.setdefault(key, {}).update(parsed)
                break
        else:
            if asset.kind is AssetKind.DEPENDENCY_MANIFEST:
                report.notes.append(
                    f"No {eco.name} lockfile was found beside `{asset.path}`, so manifest/lock "
                    "drift could not be checked."
                )


def note_skipped_scanners(report: SupplyChainReport | None, skipped: list[tuple[str, str]]) -> None:
    """Record the supply-chain scanners that did not run, on the report.

    Only this stage's scanners. A missing ruff is a fact about the language pass
    and belongs in its log, not in a section about dependencies and infrastructure.
    """
    if report is None:
        return
    for name, reason in skipped:
        if name in SCANNERS:
            report.notes.append(f"Scanner `{name}` did not run: {reason}")


def attach_scanner_findings(
    report: SupplyChainReport | None, findings: list[Finding], *, max_findings: int
) -> None:
    """Attach whole-asset scanner facts without sending them through line validation."""
    if report is None or not findings:
        return
    ordered = sorted(
        findings,
        key=lambda finding: (
            -finding.severity.rank,
            -finding.confidence,
            finding.file,
            finding.rule_id or "",
        ),
    )
    if len(ordered) > max_findings:
        report.truncated = True
        report.notes.append(
            f"{len(ordered)} scanner findings were found; the first {max_findings} are listed "
            "(`review.max_findings`)."
        )
    report.scanner_findings.extend(ordered[:max_findings])


def supported_ecosystems() -> list[str]:
    """The ecosystems with a parser, for docs and for the ``--help`` text."""
    return [eco.name for eco in ECOSYSTEMS]
