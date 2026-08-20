"""Which channel a finding is routed to.

Routing is the one thing all four surfaces have to agree on: what the terminal
groups under a heading is what the summary collapses into a section is what the
publisher declines to post inline.
"""

from __future__ import annotations

import pytest

from roborak.core.buckets import (
    BUCKET_ORDER,
    SUMMARY_BUCKETS,
    Bucket,
    bucket_for,
    by_file,
    can_anchor,
    group,
)
from roborak.core.models import ChangedFile, ChangeSet, Finding, Hunk, ReviewResult
from roborak.core.severity import Category, Kind, Severity


def changeset_covering(*lines: int) -> ChangeSet:
    """A one-file diff whose hunk carries exactly ``lines``."""
    return ChangeSet(
        files=[
            ChangedFile(
                path="app.py",
                hunks=[
                    Hunk(
                        old_start=1,
                        old_lines=len(lines),
                        new_start=min(lines),
                        new_lines=len(lines),
                        content="",
                        line_map={line: i for i, line in enumerate(lines, start=1)},
                        added_lines=set(lines),
                    )
                ],
            )
        ]
    )


def finding(line: int = 10, *, kind: Kind = Kind.POTENTIAL_ISSUE, path: str = "app.py") -> Finding:
    return Finding(
        file=path,
        start_line=line,
        end_line=line,
        severity=Severity.MAJOR,
        category=Category.BUG,
        kind=kind,
        title=f"Something at {line}",
        body="It is wrong.",
    )


@pytest.mark.parametrize(
    ("kind", "line", "expected"),
    [
        (Kind.POTENTIAL_ISSUE, 10, Bucket.ACTIONABLE),
        (Kind.NITPICK, 10, Bucket.NITPICK),
        (Kind.POTENTIAL_ISSUE, 900, Bucket.OUTSIDE_DIFF),
        # Unanchorable beats nitpick: nobody can post it inline either way.
        (Kind.NITPICK, 900, Bucket.OUTSIDE_DIFF),
        # A gap has no line by nature, so it is never "outside" the diff.
        (Kind.REQUIREMENT_GAP, 900, Bucket.REQUIREMENT_GAP),
    ],
)
def test_routing(kind, line, expected):
    assert bucket_for(finding(line, kind=kind), changeset_covering(10, 11)) is expected


def test_a_file_with_no_diff_has_nothing_to_be_outside_of():
    """Reviewing whole files (``origin="paths"``) produces no hunks at all, so
    calling a finding "outside the diff range" there would be nonsense."""
    whole_file = ChangeSet(files=[ChangedFile(path="app.py", new_content="x = 1\n")])
    assert bucket_for(finding(900), whole_file) is Bucket.ACTIONABLE


def test_a_finding_on_a_file_nobody_changed_is_outside_the_diff():
    assert bucket_for(finding(10, path="other.py"), changeset_covering(10)) is Bucket.OUTSIDE_DIFF


def test_no_changeset_means_no_anchoring_question():
    assert bucket_for(finding(900), None) is Bucket.ACTIONABLE


def test_can_anchor_follows_the_diff_position_not_the_line_number():
    changeset = changeset_covering(10, 11)
    assert can_anchor(finding(10), changeset)
    assert not can_anchor(finding(12), changeset)


def test_group_omits_empty_buckets_and_keeps_report_order():
    result = ReviewResult(
        changeset=changeset_covering(10, 11),
        findings=[finding(11, kind=Kind.NITPICK), finding(10)],
    )
    grouped = group(result)
    assert list(grouped) == [Bucket.ACTIONABLE, Bucket.NITPICK]
    assert [f.start_line for f in grouped[Bucket.ACTIONABLE]] == [10]


def test_group_orders_buckets_consistently():
    """Rendering order is fixed, so two surfaces cannot disagree about it."""
    result = ReviewResult(
        changeset=changeset_covering(10),
        findings=[
            finding(900, kind=Kind.NITPICK),
            finding(10),
            finding(10, kind=Kind.REQUIREMENT_GAP),
        ],
    )
    assert list(group(result)) == [b for b in BUCKET_ORDER if b in group(result)]
    assert Bucket.ACTIONABLE not in SUMMARY_BUCKETS


def test_by_file_preserves_the_order_it_was_given():
    findings = [finding(10, path="b.py"), finding(11, path="a.py"), finding(12, path="b.py")]
    files = by_file(findings)
    assert list(files) == ["b.py", "a.py"]
    assert [f.start_line for f in files["b.py"]] == [10, 12]
