"""Post-filters that decide which findings a human actually sees.

Most of roborak's usefulness is negative: a review that comments on unchanged
lines, repeats itself, or hedges at low confidence gets switched off within a
week. Everything here exists to prevent that.
"""

from __future__ import annotations

import logging

from roborak.core.config import Config
from roborak.core.models import ChangeSet, Finding
from roborak.core.severity import Kind, Severity

log = logging.getLogger(__name__)


def validate(findings: list[Finding], changeset: ChangeSet, config: Config) -> list[Finding]:
    """Apply every filter, in the order that discards the most obvious junk first."""
    kept = anchor_to_changed_lines(findings, changeset, full_file=config.review.full_file)
    kept = enforce_evidence(kept, require=config.review.require_evidence)
    kept = [f for f in kept if f.confidence >= config.review.min_confidence]
    kept = [f for f in kept if f.severity.at_least(config.review.severity_floor)]
    kept = dedupe(kept)
    kept = sorted(kept, key=lambda f: (-f.severity.rank, -f.confidence, f.file, f.start_line))
    return kept[: config.review.max_findings]


UNPROVEN_KINDS = frozenset({Kind.POTENTIAL_ISSUE, Kind.VERIFICATION_NEEDED})
"""The kinds that assert a defect exists, and so have something to prove.

A refactor suggestion or a nitpick claims taste, not breakage. A requirement gap
is judged against the issue text, which is evidence roborak was handed rather than
evidence it has to derive.
"""


def enforce_evidence(findings: list[Finding], *, require: bool = True) -> list[Finding]:
    """Stop an unproven model claim from reaching blocker level.

    ``confidence`` is the model grading its own homework, so a 0.95 on a guess
    reads exactly like a 0.95 on a traced failure -- and a blocker-level guess is
    the most expensive thing a review can produce, because it fails the build. A
    claim that cannot say what makes it true is demoted rather than discarded: it
    may well be real, so it becomes something to go and check.

    Static findings are exempt because a tool ran; the model reasoning about them
    afterwards is not what put them here.
    """
    if not require:
        return findings

    for finding in findings:
        if finding.source != "llm" or finding.evidence.proven:
            continue
        if finding.kind not in UNPROVEN_KINDS:
            continue
        if not finding.severity.at_least(Severity.MAJOR):
            continue
        log.debug(
            "demoting unproven %s finding to verification_needed: %s",
            finding.severity,
            finding.location,
        )
        finding.severity = Severity.MINOR
        finding.kind = Kind.VERIFICATION_NEEDED

    return findings


def anchor_to_changed_lines(
    findings: list[Finding], changeset: ChangeSet, *, full_file: bool = False
) -> list[Finding]:
    """Drop findings that do not point at a line this change touched.

    Also snaps a near-miss onto the nearest added line: models are reliable about
    *which* problem exists and slightly less reliable about the exact line, and a
    comment two lines off is still useful where a discarded one is not.
    """
    kept: list[Finding] = []

    for finding in findings:
        file = changeset.file_by_path(finding.file)
        if file is None:
            log.debug("dropping finding for file not in changeset: %s", finding.file)
            continue

        if finding.kind is Kind.REQUIREMENT_GAP:
            kept.append(finding)
            continue

        added = file.added_lines
        if not added:
            continue

        if full_file:
            kept.append(finding)
            continue

        span = range(finding.start_line, finding.end_line + 1)
        if any(line in added for line in span):
            kept.append(finding)
            continue

        nearest = min(added, key=lambda line: abs(line - finding.start_line))
        if abs(nearest - finding.start_line) <= 3:
            shift = nearest - finding.start_line
            finding.start_line += shift
            finding.end_line += shift
            kept.append(finding)
        else:
            log.debug(
                "dropping finding anchored outside the diff: %s (nearest added line %d)",
                finding.location,
                nearest,
            )

    return kept


def dedupe(findings: list[Finding]) -> list[Finding]:
    """Collapse findings that say the same thing, keeping the most useful copy.

    Static analysis and the LLM routinely both notice the same problem; the LLM's
    version explains the consequence, so it wins on a tie.
    """
    best: dict[str, Finding] = {}

    for finding in findings:
        key = finding.fingerprint
        incumbent = best.get(key)
        if incumbent is None or _preference(finding) > _preference(incumbent):
            best[key] = finding

    out: list[Finding] = []
    for finding in sorted(best.values(), key=lambda f: (f.file, f.start_line, -f.severity.rank)):
        if any(_overlaps(finding, existing) for existing in out):
            continue
        out.append(finding)
    return out


def _overlaps(a: Finding, b: Finding) -> bool:
    if Kind.REQUIREMENT_GAP in {a.kind, b.kind}:
        return False
    return (
        a.file == b.file
        and a.category == b.category
        and a.start_line <= b.end_line
        and b.start_line <= a.end_line
    )


def _preference(finding: Finding) -> tuple[int, int, float]:
    """Rank duplicates: explanation beats detection, then severity, then confidence."""
    has_explanation = 1 if finding.source == "llm" else 0
    return (has_explanation, finding.severity.rank, finding.confidence)
