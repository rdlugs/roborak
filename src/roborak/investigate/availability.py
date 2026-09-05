"""Whether the tree in front of us is the code under review.

The investigation stage reads the checkout. That is safe for a local change,
because the checkout *is* the change, and unsafe for a forge change, because the
working tree may hold an unrelated branch. Reading it anyway would let roborak
confirm or drop a finding on the strength of code that is not in the merge
request -- the most expensive mistake this stage could make.

The blast-radius pass answers a weaker version of this question with
``git cat-file -e`` (``context/impact.py``), which proves the head commit was
*fetched* rather than *checked out*, and then labels its own results as possibly
stale. That is the right trade for a context pass whose output is prose. It is
the wrong trade here, where the output moves a severity, so this module asks for
equality instead.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from roborak.context.impact import _git
from roborak.core.models import ChangeSet, InvestigationStatus
from roborak.verify.runner import CHECKED_OUT_ORIGINS

log = logging.getLogger(__name__)


@dataclass
class Availability:
    """What the stage is allowed to do against this checkout."""

    reads_working_tree: bool = False
    """Whether a file read may open the tree. When false, reads fall back to the
    forge-supplied content already carried on the changeset."""

    searches: bool = False
    """Whether ``git grep`` may run. A search has no changeset-backed fallback:
    the whole point is to look outside the diff."""

    reads_changeset: bool = False
    """Whether reads may be served from the forge-supplied content on the
    changeset. This is the degraded mode for a review whose checkout is not the
    code under review: the change's own files can still be read, because that
    content came from the forge, but nothing outside the diff can."""

    status: InvestigationStatus = InvestigationStatus.UNAVAILABLE
    notes: list[str] = field(default_factory=list)

    @property
    def usable(self) -> bool:
        """Whether anything at all can be gathered."""
        return self.reads_working_tree or self.searches or self.reads_changeset


def _degraded(changeset: ChangeSet, note: str) -> Availability:
    """Reads from forge-supplied content, when the change carries any.

    A checkout we do not trust is not the same as no evidence at all: the files
    the change itself touches arrived with it, and reading those answers a real
    share of the questions a candidate raises. What it cannot answer is anything
    outside the diff, which is why ``searches`` stays off.
    """
    has_content = any(file.new_content is not None for file in changeset.files)
    if not has_content:
        return Availability(notes=[note])
    return Availability(
        reads_changeset=True,
        status=InvestigationStatus.PARTIAL,
        notes=[f"{note} Only the files carried by the change were readable."],
    )


def resolve(changeset: ChangeSet, repo: Path) -> Availability:
    """What may be read for this change, and why not, when the answer is nothing."""
    if changeset.origin in CHECKED_OUT_ORIGINS:
        # The working tree is what the user asked about, uncommitted edits included.
        return Availability(
            reads_working_tree=True,
            searches=True,
            status=InvestigationStatus.COMPLETED,
        )

    head = changeset.head_sha
    if not head:
        return _degraded(
            changeset, "the change does not name a head commit, so the checkout cannot be matched."
        )

    checked_out = (_git(repo, "rev-parse", "HEAD") or "").strip()
    if not checked_out:
        return _degraded(changeset, "no git checkout was available to investigate against.")
    if checked_out != head:
        log.debug("checkout %s does not match reviewed head %s", checked_out[:12], head[:12])
        return _degraded(
            changeset,
            f"the checkout is at {checked_out[:12]} but the change is at {head[:12]}, "
            "so an unrelated tree was never read.",
        )

    # Matching HEAD is not enough on its own: a dirty tree is a different file on
    # disk from the one the change proposes, and it is the file we would open.
    dirty = _git(repo, "status", "--porcelain")
    if dirty is None:
        return _degraded(changeset, "the checkout state could not be read, so it was not trusted.")
    if dirty.strip():
        return _degraded(
            changeset,
            "the checkout matches the reviewed head but has uncommitted changes, "
            "so it was not read.",
        )

    return Availability(
        reads_working_tree=True,
        searches=True,
        status=InvestigationStatus.COMPLETED,
    )
