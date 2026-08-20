"""Which channel a finding belongs on.

CodeRabbit's routing, which roborak follows: a finding that points at a changed
line and is worth interrupting for goes inline on the diff, where the author is
already looking. A nitpick is folded into the summary instead, so that the small
stuff cannot drown the review. And one that cannot be anchored is *reported* in
the summary rather than discarded -- roborak used to count those as failures and
show them only in the terminal, which meant a reviewer reading the merge request
never learned they existed.

Bucketing lives here, above both ``render`` and ``publish``, because all four
surfaces have to agree on it: what the terminal groups under a heading is what
the summary comment collapses into a section is what the publisher declines to
post inline.
"""

from __future__ import annotations

from enum import StrEnum

from roborak.core.models import ChangeSet, Finding, ReviewResult
from roborak.core.severity import Kind


class Bucket(StrEnum):
    ACTIONABLE = "actionable"
    """Anchorable and worth an inline comment."""

    OUTSIDE_DIFF = "outside_diff"
    """Real, but pointing somewhere the diff does not cover."""

    NITPICK = "nitpick"
    """Worth saying once, quietly; never worth an inline thread."""

    REQUIREMENT_GAP = "requirement_gap"
    """Something the issue asked for that the change does not do."""


BUCKET_TITLE: dict[Bucket, str] = {
    Bucket.ACTIONABLE: "Actionable comments",
    Bucket.OUTSIDE_DIFF: "⚠️ Outside diff range comments",
    Bucket.NITPICK: "🧹 Nitpick comments",
    Bucket.REQUIREMENT_GAP: "🔍 Requirements not met",
}

BUCKET_PLAIN: dict[Bucket, str] = {
    Bucket.ACTIONABLE: "Actionable comments",
    Bucket.OUTSIDE_DIFF: "Outside diff comments",
    Bucket.NITPICK: "Nitpick comments",
    Bucket.REQUIREMENT_GAP: "Requirements not met",
}
"""The same names without the badge, for places an emoji would only be in the
way -- a fenced block a coding agent reads, or a plain-text report."""

# Rendering order: what blocks a merge first, what was nearly missed next, then
# the small stuff.
BUCKET_ORDER = (
    Bucket.ACTIONABLE,
    Bucket.REQUIREMENT_GAP,
    Bucket.OUTSIDE_DIFF,
    Bucket.NITPICK,
)

SUMMARY_BUCKETS = (Bucket.REQUIREMENT_GAP, Bucket.OUTSIDE_DIFF, Bucket.NITPICK)
"""The buckets a forge summary carries, because no inline thread can hold them."""


def can_anchor(finding: Finding, changeset: ChangeSet) -> bool:
    """Whether a forge can attach this finding to a line of the diff.

    Both forges anchor by position within the diff body rather than by file line,
    so a finding on a line the diff does not carry has nowhere to go.
    """
    file = changeset.file_by_path(finding.file)
    return file is not None and file.diff_position(finding.start_line) is not None


def bucket_for(finding: Finding, changeset: ChangeSet | None) -> Bucket:
    """Route one finding.

    Order matters. A gap has no line by nature, so it is never "outside" the
    diff -- it is simply not about a line. Anchorability is asked next, because a
    nitpick nobody can post inline is still a nitpick nobody can post inline.
    """
    if finding.kind is Kind.REQUIREMENT_GAP:
        return Bucket.REQUIREMENT_GAP
    if _is_outside_diff(finding, changeset):
        return Bucket.OUTSIDE_DIFF
    if finding.kind is Kind.NITPICK:
        return Bucket.NITPICK
    return Bucket.ACTIONABLE


def _is_outside_diff(finding: Finding, changeset: ChangeSet | None) -> bool:
    """Whether the finding misses a diff that exists.

    A file reviewed whole -- ``roborak`` reviewing plain paths, where there are no
    hunks at all -- has no diff for a finding to fall outside of, so calling one
    "outside the diff range" there would be nonsense rather than a warning.
    """
    if changeset is None:
        return False
    file = changeset.file_by_path(finding.file)
    if file is None:
        # A file the change never touched: there is no diff of it at all, so
        # nothing could anchor this even in principle.
        return True
    if not file.hunks:
        # A file reviewed whole instead of as a diff, which is what reviewing
        # plain paths produces. There is no range for a finding to fall outside.
        return False
    return not can_anchor(finding, changeset)


def group(result: ReviewResult) -> dict[Bucket, list[Finding]]:
    """Every finding, bucketed, in report order. Empty buckets are omitted."""
    grouped: dict[Bucket, list[Finding]] = {}
    for finding in result.sorted_findings():
        grouped.setdefault(bucket_for(finding, result.changeset), []).append(finding)
    return {bucket: grouped[bucket] for bucket in BUCKET_ORDER if bucket in grouped}


def by_file(findings: list[Finding]) -> dict[str, list[Finding]]:
    """Group within a bucket, the way a reviewer reads: one file at a time.

    Insertion order is the caller's sort order, so the most severe file leads.
    """
    files: dict[str, list[Finding]] = {}
    for finding in findings:
        files.setdefault(finding.file, []).append(finding)
    return files
