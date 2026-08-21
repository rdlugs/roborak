"""The review as markdown, in the shape CodeRabbit posts.

One renderer serves both ``--markdown out.md`` and the comment ``--post``
publishes, so what you preview locally is what reviewers see. They differ only in
which buckets they carry: the forge comment leaves out the findings it already
posted as inline threads, while the local report, having no inline channel, keeps
everything.

The structure is GitHub-flavored markdown rather than plain: collapsible
``<details>`` sections are what keep a review with thirty nitpicks in it from
burying the two findings that matter.
"""

from __future__ import annotations

import textwrap

from roborak.core.buckets import (
    BUCKET_PLAIN,
    BUCKET_TITLE,
    Bucket,
    by_file,
    group,
)
from roborak.core.models import Finding, ReviewResult
from roborak.core.severity import (
    CATEGORY_LABEL,
    EFFORT_LABEL,
    SEVERITY_LABEL,
    Kind,
    Severity,
)
from roborak.render.lexers import lexer_for
from roborak.render.prompt_only import (
    AGENT_PREAMBLE,
    agent_instruction,
    agent_instruction_body,
)

FINGERPRINT_PREFIX = "roborak:v1"
FINGERPRINT_V2_PREFIX = "roborak:v2"
"""Marks a rendered finding with its identity, the way CodeRabbit's
``cr-comment:v1`` marker does. Invisible to a reader, but it means a published
review carries a record of itself that does not depend on local state."""
REVIEW_MARKER = "roborak:review"

# Sections that are collapsed by default. Everything else is a banner or a table
# the reader should not have to open.
_COLLAPSED = (Bucket.ACTIONABLE, Bucket.REQUIREMENT_GAP, Bucket.NITPICK)


def render(result: ReviewResult, *, collapsible: bool = True) -> str:
    """The whole review, as one document.

    There is deliberately no way to render a subset. This is what the terminal
    shows, what ``--markdown`` writes and what ``--post`` publishes as the
    comment, and the only way three surfaces cannot disagree about a review is
    for there to be one rendering of it.

    ``collapsible=False`` opens the ``<details>`` sections out into headings.
    A terminal cannot fold a section, and ``rich.Markdown`` drops HTML silently
    -- which would take every section heading with it and leave the reader bare
    lists with no idea what they were. Same document, same order, same words;
    only the way a section folds changes.
    """
    grouped = group(result)

    sections = [_header(result)]

    if walkthrough := result.walkthrough:
        if walkthrough.overview:
            sections.append(walkthrough.overview.strip())
        if walkthrough.file_summaries:
            sections.append(_walkthrough_table(result))
        if walkthrough.sequence_diagram:
            sections.append(
                "### Flow\n\n```mermaid\n" + walkthrough.sequence_diagram.strip() + "\n```"
            )

    if not grouped:
        sections.append(_nothing_to_report(result))
    else:
        sections.append(_severity_table(grouped))
        # The banner first: a finding nobody can anchor is the one most easily
        # lost, so it is the one thing here that is not collapsed.
        if outside := grouped.get(Bucket.OUTSIDE_DIFF):
            sections.append(_outside_diff_callout(outside, collapsible=collapsible))
        for bucket in _COLLAPSED:
            if findings := grouped.get(bucket):
                sections.append(_bucket_section(bucket, findings, collapsible=collapsible))
        sections.append(_global_agent_prompt(grouped, collapsible=collapsible))

    sections.append(f"<!-- {REVIEW_MARKER} -->")
    sections.append("---")
    sections.append(_review_info(result, collapsible=collapsible))

    return "\n\n".join(section for section in sections if section) + "\n"


# -- heading and overview --------------------------------------------------


def _header(result: ReviewResult) -> str:
    title = "# Code review"
    walkthrough = result.walkthrough
    if walkthrough and walkthrough.title:
        title = f"# {walkthrough.title}"
    elif result.changeset and result.changeset.title:
        title = f"# {result.changeset.title}"

    meta: list[str] = []
    changeset = result.changeset
    # An empty changeset has nothing to describe; "0 file(s) changed" would be a
    # header that says less than nothing.
    if changeset and not changeset.is_empty:
        if changeset.head_ref and changeset.base_ref:
            meta.append(f"`{changeset.head_ref}` → `{changeset.base_ref}`")
        meta.append(f"{len(changeset.files)} file(s) changed")
    if result.issue is not None:
        reference = result.issue.reference
        link = f"[{reference}]({result.issue.web_url})" if result.issue.web_url else reference
        meta.append(f"against {link}")
    if walkthrough and walkthrough.estimated_effort:
        meta.append(f"review effort {walkthrough.estimated_effort}/5")
    if walkthrough and walkthrough.labels:
        meta.append(" ".join(f"`{label}`" for label in walkthrough.labels))

    return f"{title}\n\n{' · '.join(meta)}" if meta else title


def _walkthrough_table(result: ReviewResult) -> str:
    assert result.walkthrough is not None
    rows = ["### Walkthrough", "", "| File | Change |", "| --- | --- |"]
    rows += [
        f"| `{summary.path}` | {_escape_cell(summary.summary)} |"
        for summary in result.walkthrough.file_summaries
    ]
    return "\n".join(rows)


def _nothing_to_report(result: ReviewResult) -> str:
    """Say the right nothing.

    "No findings" is a claim about the code; it is the wrong thing to tell someone
    whose working tree simply had nothing in it to look at.
    """
    if result.changeset is not None and result.changeset.is_empty:
        return "No changes to review."
    return "No findings. ✅"


def _severity_table(grouped: dict[Bucket, list[Finding]]) -> str:
    findings = [f for bucket in grouped.values() for f in bucket]
    counts = {severity: 0 for severity in Severity}
    for finding in findings:
        counts[finding.severity] += 1

    total = len(findings)
    rows = [
        f"### {total} finding{'s' if total != 1 else ''}",
        "",
        "| Severity | Count |",
        "| --- | --- |",
    ]
    rows += [
        f"| {SEVERITY_LABEL[severity]} | {counts[severity]} |"
        for severity in Severity
        if counts[severity]
    ]
    return "\n".join(rows)


# -- buckets ---------------------------------------------------------------


def _outside_diff_callout(findings: list[Finding], *, collapsible: bool) -> str:
    """The one section that opens itself, wrapped in a GitHub warning callout."""
    inner = _bucket_section(Bucket.OUTSIDE_DIFF, findings, collapsible=collapsible)
    note = (
        "Some comments are outside the diff and can't be posted inline due to platform limitations."
    )
    if not collapsible:
        # rich shows `[!CAUTION]` as literal text, and quoting a section this
        # long is a wall of bar. The heading already carries the warning badge.
        return f"> {note}\n\n{inner}"
    return _callout("CAUTION", f"{note}\n\n{inner}")


def _bucket_section(bucket: Bucket, findings: list[Finding], *, collapsible: bool) -> str:
    files = by_file(findings)
    blocks = [
        _details(
            f"{path} ({len(items)})",
            _findings_block(items, collapsible=collapsible),
            nested=True,
            level=3,
            collapsible=collapsible,
        )
        for path, items in files.items()
    ]
    return _details(
        f"{BUCKET_TITLE[bucket]} ({len(findings)})",
        "\n\n".join(blocks),
        nested=True,
        level=2,
        collapsible=collapsible,
    )


def _findings_block(findings: list[Finding], *, collapsible: bool) -> str:
    """Findings within one file, rule-separated the way CodeRabbit separates them."""
    return "\n\n---\n\n".join(
        finding_markdown(finding, collapsible=collapsible) for finding in findings
    )


def finding_markdown(
    finding: Finding, *, suggestion_syntax: str = "suggestion", collapsible: bool = True
) -> str:
    """One finding, as it appears in a report *and* as an inline review comment.

    The publishers render inline threads with this too, so a comment on the diff
    carries the same badges, the same agent prompt and the same identity marker
    as its counterpart in the summary. ``suggestion_syntax`` is the fenced-block
    language the forge renders as a one-click apply -- GitLab wants a line-range
    spec appended, which the caller supplies.
    """
    badges = " | ".join(
        f"_{label}_"
        for label in (
            CATEGORY_LABEL[finding.category],
            SEVERITY_LABEL[finding.severity],
            EFFORT_LABEL[finding.effort],
        )
    )
    # A requirement gap's line is nominal; printing a range would imply an anchor
    # it does not have.
    lead = badges if finding.kind is Kind.REQUIREMENT_GAP else f"`{_span(finding)}`: {badges}"

    lines = [lead, "", f"**{finding.title.rstrip('.')}.**", "", finding.body.strip()]

    if finding.suggestion:
        # A forge turns ```suggestion into a one-click apply. A terminal has no
        # such trick and would render it uncoloured, so name the real language
        # there and let rich highlight it.
        fence = suggestion_syntax if collapsible else lexer_for(finding.file)
        lines += ["", f"```{fence}", finding.suggestion.rstrip(), "```"]

    lines += [
        "",
        _details(
            "🤖 Prompt for AI Agents",
            _fenced(_agent_prompt(finding)),
            level=4,
            collapsible=collapsible,
        ),
    ]
    lines += [
        "",
        f"<!-- {FINGERPRINT_PREFIX}:{finding.fingerprint} -->",
        f"<!-- {FINGERPRINT_V2_PREFIX}:{finding.fingerprint_v2} -->",
    ]

    if source := _source_note(finding):
        lines += ["", source]

    return "\n".join(lines)


def _span(finding: Finding) -> str:
    return f"{finding.start_line}-{finding.end_line}"


def _source_note(finding: Finding) -> str:
    """Where this came from, when it did not simply come from reading the diff."""
    if finding.rule_id and finding.source == "rule":
        return f"_Source: project rule `{finding.rule_id}`_"
    if finding.tool:
        return f"_Source: `{finding.tool}`_"
    if finding.source == "llm":
        return f"_Confidence: {finding.confidence:.0%}_"
    return ""


# -- agent prompts ---------------------------------------------------------


def _agent_prompt(finding: Finding) -> str:
    return f"{AGENT_PREAMBLE}\n\n{agent_instruction(finding)}"


def _global_agent_prompt(grouped: dict[Bucket, list[Finding]], *, collapsible: bool) -> str:
    """Every instruction in one block, so an agent can take the whole review at once."""
    parts: list[str] = []

    for bucket, findings in grouped.items():
        lines = [f"{BUCKET_PLAIN[bucket]}:"]
        for path, items in by_file(findings).items():
            lines.append(f"In `@{path}`:")
            for finding in items:
                where = (
                    f"Line {finding.start_line}"
                    if finding.start_line == finding.end_line
                    else f"Lines {_span(finding)}"
                )
                lines.append(f"- {where}: {agent_instruction_body(finding)}")
        parts.append("\n".join(lines))

    body = "\n\n---\n\n".join(parts)
    return _details(
        "🤖 Prompt for all review comments with AI agents",
        _fenced(f"{AGENT_PREAMBLE}\n\n{body}"),
        level=2,
        collapsible=collapsible,
    )


# -- review info -----------------------------------------------------------


def _review_info(result: ReviewResult, *, collapsible: bool) -> str:
    blocks: list[str] = []
    changeset = result.changeset

    config_lines: list[str] = []
    if changeset is not None:
        config_lines.append(f"**Source**: {changeset.origin}")
    if result.issue is not None:
        config_lines.append(f"**Judged against**: {result.issue.reference}")
    if config_lines:
        blocks.append(
            _details(
                "⚙️ Run configuration", "\n\n".join(config_lines), level=3, collapsible=collapsible
            )
        )

    if changeset is not None and changeset.base_sha and changeset.head_sha:
        blocks.append(
            _details(
                "📥 Commits",
                "Reviewing files that changed between "
                f"{changeset.base_sha} and {changeset.head_sha}.",
                level=3,
                collapsible=collapsible,
            )
        )

    if changeset is not None and changeset.files:
        listed = "\n".join(f"* `{file.path}`" for file in changeset.files)
        blocks.append(
            _details(
                f"📒 Files selected for processing ({len(changeset.files)})",
                listed,
                level=3,
                collapsible=collapsible,
            )
        )

    if result.coverage:
        listed = "\n".join(
            f"* `{item.path}` — {item.reason.value.replace('_', ' ')}"
            + (f": {item.detail}" if item.detail else "")
            for item in result.coverage
        )
        blocks.append(
            _details(
                f"🚧 Review coverage ({len(result.coverage)} omission(s))",
                listed,
                level=3,
                collapsible=collapsible,
            )
        )
    elif result.skipped_files:
        listed = "\n".join(f"* `{path}`" for path in result.skipped_files)
        blocks.append(
            _details(
                f"🚧 Files skipped (context budget) ({len(result.skipped_files)})",
                listed,
                level=3,
                collapsible=collapsible,
            )
        )

    if result.errors:
        blocks.append(
            _details(
                "❌ Errors",
                "\n".join(f"* {error}" for error in result.errors),
                level=3,
                collapsible=collapsible,
            )
        )

    if not blocks:
        return ""
    return _details("ℹ️ Review info", "\n\n".join(blocks), level=2, collapsible=collapsible)


# -- markup helpers --------------------------------------------------------


def _details(
    summary: str, body: str, *, nested: bool = False, level: int = 4, collapsible: bool = True
) -> str:
    """One section, collapsible where the reader can collapse it.

    ``nested=True`` wraps the body in a ``<blockquote>``, which is what lets a
    ``<details>`` contain another one -- without it GitHub stops rendering the
    markdown inside. Leaf sections skip it: a quote bar running down the side of
    a fenced code block is just noise.

    ``collapsible=False`` is the terminal form: a heading at ``level``, since
    nothing there can fold and the summary line is the only thing telling the
    reader what they are looking at.
    """
    if not collapsible:
        return f"{'#' * level} {summary}\n\n{body}"
    if nested:
        return (
            f"<details>\n<summary>{summary}</summary><blockquote>"
            f"\n\n{body}\n\n</blockquote></details>"
        )
    return f"<details>\n<summary>{summary}</summary>\n\n{body}\n\n</details>"


def _callout(kind: str, body: str) -> str:
    quoted = "\n".join(f"> {line}".rstrip() for line in body.splitlines())
    return f"> [!{kind}]\n{quoted}"


FENCE_WIDTH = 80
"""Agent prompts are wrapped rather than left as one long line: they sit inside a
fenced block, which does not wrap, so an unwrapped instruction becomes a
horizontal scrollbar on the merge request."""


def _fenced(text: str) -> str:
    return f"```\n{_wrap(text)}\n```"


def _wrap(text: str) -> str:
    lines: list[str] = []
    for line in text.splitlines():
        if not line.strip():
            lines.append("")
            continue
        # `- ` items keep their marker on the first line and hang underneath.
        indent = "  " if line.startswith("- ") else ""
        lines.extend(
            textwrap.wrap(
                line,
                width=FENCE_WIDTH,
                subsequent_indent=indent,
                break_long_words=False,
                break_on_hyphens=False,
            )
            or [""]
        )
    return "\n".join(lines)


def _escape_cell(text: str) -> str:
    """Keep a summary from breaking out of its table cell."""
    return " ".join(text.split()).replace("|", "\\|")
