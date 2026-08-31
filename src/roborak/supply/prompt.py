"""The report as evidence for the model.

Deliberately asymmetric, the way ``verify.runner.for_prompt`` is. A dependency
that moved carries everything known about it, because that is the whole content of
the stage. The lockfile it came from carries nothing: it is generated data whose
exclusion from the prompt is the reason this module exists.

``None`` means the stage never ran, and nothing else does. A report saying
``nothing_relevant`` is a different fact -- it says we looked and this change does
not touch a dependency, a workflow, a container or any infrastructure -- and the
model should not have to infer that from silence.
"""

from __future__ import annotations

from typing import Any

from roborak.core.models import SupplyChainReport, SupplyChainStatus


def for_prompt(report: SupplyChainReport | None) -> dict[str, Any] | None:
    """The report as a plain dict, or ``None`` when the stage never ran.

    Also ``None`` for ``nothing_relevant``: a change that touches no boundary has
    no section to render and no checklist to gate on, and spending prompt tokens
    saying "there were no dependency changes" on every ordinary diff would be a
    permanent tax for an empty result.
    """
    if report is None or report.status is SupplyChainStatus.NOTHING_RELEVANT:
        return None
    return {
        "status": report.status.value,
        "analysed": report.analysed,
        "truncated": report.truncated,
        "kinds": sorted(kind.value for kind in report.kinds()),
        "ecosystems": list(report.ecosystems),
        "notes": list(report.notes),
        "assets": [{"path": asset.path, "kind": asset.kind.value} for asset in report.assets],
        "changes": [
            {
                "ecosystem": change.ecosystem,
                "name": change.name,
                "kind": change.kind.value,
                "versions": change.display_version,
                "source": _source_of(change.old_source, change.new_source),
                "direct": change.direct,
                "note": change.note,
            }
            for change in report.changes
        ],
    }


def _source_of(old: str, new: str) -> str:
    """Where a package resolves from, shown as a move only when it moved."""
    if old and new and old != new:
        return f"{old} → {new}"
    return new or old
