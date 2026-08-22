"""The review as markdown, in the shape a forge comment takes.

One renderer serves both ``--markdown out.md`` and the comment ``--post``
publishes, so what you preview locally is what reviewers see. They differ only in
which buckets they carry: the forge comment leaves out the findings it already
posted as inline threads, while the local report, having no inline channel, keeps
everything.

The structure is GitHub-flavored markdown rather than plain: collapsible
``<details>`` sections are what keep a review with thirty nitpicks in it from
burying the two findings that matter.

``Form.TERMINAL`` is the same document for a reader who cannot fold a section.
Nothing there can collapse, so the sections written for a machine -- the agent
prompts, the review-info tree -- are left out instead of opened out, and a
one-line footer carries what a reader would otherwise lose. ``full=True`` puts
them back. Every finding, with its badges, its body and its fix, is in both
forms: the terminal may show less *about* the run, never less of the review.
"""

from __future__ import annotations

import base64
import textwrap
import zlib
from collections.abc import Iterable
from enum import StrEnum
from pathlib import Path

from pydantic import ValidationError

from roborak.core.buckets import (
    BUCKET_PLAIN,
    BUCKET_TITLE,
    Bucket,
    by_file,
    group,
)
from roborak.core.models import Finding, ReviewResult, Walkthrough
from roborak.core.severity import (
    CATEGORY_LABEL,
    EFFORT_LABEL,
    SEVERITY_LABEL,
    Kind,
    Severity,
)
from roborak.render import snippet
from roborak.render.lexers import lexer_for
from roborak.render.prompt_only import (
    AGENT_PREAMBLE,
    agent_instruction,
    agent_instruction_body,
)

FINGERPRINT_PREFIX = "roborak:v1"
FINGERPRINT_V2_PREFIX = "roborak:v2"
"""Marks a rendered finding with its identity. Invisible to a reader, but it
means a published review carries a record of itself that does not depend on
local state."""
REVIEW_MARKER = "roborak:review"
FLOW_MARKER_PREFIX = "roborak:flow"
WALKTHROUGH_MARKER_PREFIX = "roborak:walkthrough"
"""Carries the structured overview along with the comment that renders it.

The overview is reused rather than paid for again whenever the shape of the
change has not moved, and until now the only copy lived in this machine's
state directory. Any other machine -- a colleague's, or CI, which starts from
an empty checkout every run -- had no way to reproduce the narrative, and so
published a review without it or, on a clean run, published nothing at all.
The comment the overview was rendered into is the one place every machine can
already read, so it is where the overview travels."""
"""Stamps the published summary with the shape of the change it narrates, so a
later run can tell whether the overview still holds without asking the model."""

LOGO_URL = "https://raw.githubusercontent.com/rdlugs/roborak/main/assets/roborak_128.png"
"""Where a published report finds the icon.

Pinned to ``main`` rather than a tag, because a comment posted a year ago should
still render. Absolute because the comment lives on a forge that has no idea where
this repository's files are.
"""


class Form(StrEnum):
    """Who the document is being rendered for."""

    PUBLISHED = "published"
    """``<details>`` HTML: ``--markdown``, ``--post``, and a redirected stdout."""

    TERMINAL = "terminal"
    """Headings, and only the sections a person reading a terminal wants."""


CONTEXT_FENCE = "roborak-context"
"""Info string of the fence carrying a finding's surrounding code.

``roborak-context <snippet start> <flagged from> <flagged to> <path>``, with the
raw lines as the body. ``render.rich_report`` expands it into a gutter-numbered,
highlighted block. It is the one thing in a rendered document that is not
meaningful markdown, which is only tolerable because ``Form.TERMINAL`` is never
written to a file and never published -- see ``render()``.
"""


_COLLAPSED = (Bucket.ACTIONABLE, Bucket.REQUIREMENT_GAP, Bucket.NITPICK)


def render(
    result: ReviewResult,
    *,
    form: Form = Form.PUBLISHED,
    repo: Path | None = None,
    full: bool = False,
) -> str:
    """The whole review, as one document.

    There is deliberately no way to render a subset of the *findings*. This is
    what the terminal shows, what ``--markdown`` writes and what ``--post``
    publishes as the comment, and the only way three surfaces cannot disagree
    about a review is for there to be one rendering of it.

    ``Form.TERMINAL`` opens the ``<details>`` sections out into headings, because
    a terminal cannot fold one and ``rich.Markdown`` drops HTML silently -- which
    would take every section heading with it and leave the reader bare lists with
    no idea what they were. It also leaves out the sections written for a machine
    rather than a reader, since opened out they bury the review; ``full=True``
    restores them. ``repo`` lets each finding carry the lines it points at, read
    from the working tree.
    """
    collapsible = form is Form.PUBLISHED
    machine_sections = collapsible or full
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
        if outside := grouped.get(Bucket.OUTSIDE_DIFF):
            sections.append(_bucket_section_with_callout(outside, form=form, repo=repo, full=full))
        for bucket in _COLLAPSED:
            if findings := grouped.get(bucket):
                sections.append(_bucket_section(bucket, findings, form=form, repo=repo, full=full))
        if machine_sections:
            sections.append(_global_agent_prompt(grouped, collapsible=collapsible))

    if form is Form.PUBLISHED:
        sections.append(f"<!-- {REVIEW_MARKER} -->")
        if result.changeset is not None and (flow := result.changeset.flow_digest):
            sections.append(f"<!-- {FLOW_MARKER_PREFIX}:{flow} -->")
        if carried := encode_walkthrough(result.walkthrough):
            sections.append(f"<!-- {WALKTHROUGH_MARKER_PREFIX}:{carried} -->")
    sections.append("---")
    if machine_sections:
        sections.append(_review_info(result, collapsible=collapsible))
    else:
        sections.append(_terminal_footer(result, hid_sections=bool(grouped)))
    sections.append(_signature(form=form))

    return "\n\n".join(section for section in sections if section) + "\n"


def _header(result: ReviewResult) -> str:
    title = "# Code review"
    walkthrough = result.walkthrough
    if walkthrough and walkthrough.title:
        title = f"# {walkthrough.title}"
    elif result.changeset and result.changeset.title:
        title = f"# {result.changeset.title}"

    meta: list[str] = []
    changeset = result.changeset
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


def _bucket_section_with_callout(
    findings: list[Finding], *, form: Form, repo: Path | None, full: bool
) -> str:
    """The one section that opens itself, wrapped in a GitHub warning callout."""
    inner = _bucket_section(Bucket.OUTSIDE_DIFF, findings, form=form, repo=repo, full=full)
    note = (
        "Some comments are outside the diff and can't be posted inline due to platform limitations."
    )
    if form is Form.TERMINAL:
        return f"> {note}\n\n{inner}"
    return _callout("CAUTION", f"{note}\n\n{inner}")


def _bucket_section(
    bucket: Bucket, findings: list[Finding], *, form: Form, repo: Path | None, full: bool
) -> str:
    files = by_file(findings)
    blocks = [
        _details(
            f"{path} ({len(items)})",
            _findings_block(items, form=form, repo=repo, full=full),
            nested=True,
            level=3,
            collapsible=form is Form.PUBLISHED,
        )
        for path, items in files.items()
    ]
    return _details(
        f"{BUCKET_TITLE[bucket]} ({len(findings)})",
        "\n\n".join(blocks),
        nested=True,
        level=2,
        collapsible=form is Form.PUBLISHED,
    )


def _findings_block(findings: list[Finding], *, form: Form, repo: Path | None, full: bool) -> str:
    """Findings within one file, separated by a rule."""
    return "\n\n---\n\n".join(
        finding_markdown(finding, form=form, repo=repo, full=full) for finding in findings
    )


def finding_markdown(
    finding: Finding,
    *,
    suggestion_syntax: str = "suggestion",
    form: Form = Form.PUBLISHED,
    repo: Path | None = None,
    full: bool = False,
) -> str:
    """One finding, as it appears in a report *and* as an inline review comment.

    The publishers render inline threads with this too, so a comment on the diff
    carries the same badges, the same agent prompt and the same identity marker
    as its counterpart in the summary. ``suggestion_syntax`` is the fenced-block
    language the forge renders as a one-click apply -- GitLab wants a line-range
    spec appended, which the caller supplies.
    """
    collapsible = form is Form.PUBLISHED
    lines = [_lead(finding, form=form), "", f"**{finding.title.rstrip('.')}.**"]

    if context := _code_context(finding, repo, form=form):
        lines += ["", context]

    lines += ["", finding.body.strip()]

    if finding.suggestion:
        fence = suggestion_syntax if collapsible else lexer_for(finding.file)
        lines += ["", f"```{fence}", finding.suggestion.rstrip(), "```"]

    if collapsible or full:
        lines += [
            "",
            _details(
                "🤖 Prompt for AI Agents",
                _fenced(_agent_prompt(finding)),
                level=4,
                collapsible=collapsible,
            ),
        ]
    if collapsible:
        lines += [
            "",
            f"<!-- {FINGERPRINT_PREFIX}:{finding.fingerprint} -->",
            f"<!-- {FINGERPRINT_V2_PREFIX}:{finding.fingerprint_v2} -->",
        ]

    if source := _source_note(finding):
        lines += ["", source]

    return "\n".join(lines)


def _lead(finding: Finding, *, form: Form) -> str:
    """The badge line: category, then severity, then what the fix will cost.

    A requirement gap's line is nominal, so it names the file and no range --
    printing one would imply an anchor it does not have.
    """
    labels = (
        CATEGORY_LABEL[finding.category],
        SEVERITY_LABEL[finding.severity],
        EFFORT_LABEL[finding.effort],
    )
    where = finding.file if finding.kind is Kind.REQUIREMENT_GAP else finding.location

    if form is Form.TERMINAL:
        return f"##### {' · '.join(labels)} · {where}"

    badges = " | ".join(f"_{label}_" for label in labels)
    return badges if finding.kind is Kind.REQUIREMENT_GAP else f"`{_span(finding)}`: {badges}"


def _code_context(finding: Finding, repo: Path | None, *, form: Form) -> str:
    """The lines the finding points at, for a reader who has them on disk.

    Terminal only: a forge shows the diff beside the comment already, and a
    published report that pasted the code back in would be repeating it.
    """
    if form is not Form.TERMINAL or repo is None or finding.kind is Kind.REQUIREMENT_GAP:
        return ""
    found = snippet.read(finding, repo)
    if found is None:
        return ""
    code, start = found
    info = f"{CONTEXT_FENCE} {start} {finding.start_line} {finding.end_line} {finding.file}"
    return f"```{info}\n{code}\n```"


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


def _signature(*, form: Form) -> str:
    """Who reviewed this.

    The one thing in the document that names its author where a reader will see
    it -- the fingerprint comments say the same thing, but only to a machine. It
    is the last section either form emits, which also means a published review can
    no longer end on a bare ``---`` when ``_review_info`` has nothing to say.

    ``Form.PUBLISHED`` gets the icon. The terminal form says the same thing in the
    characters it has: ``rich.Markdown`` drops HTML silently, and a terminal could
    not show a PNG anyway.
    """
    if form is Form.PUBLISHED:
        return (
            f'<sub><img src="{LOGO_URL}" width="14" align="top"> Reviewed by <b>roborak</b></sub>'
        )
    return "_🤖 Reviewed by roborak_"


def _terminal_footer(result: ReviewResult, *, hid_sections: bool) -> str:
    """What ``_review_info`` says, in the space a reader will actually read.

    The run's own metadata compresses to one line. What the review could *not*
    cover does not: an omitted file, a skipped file and an error are the three
    things a reader must never lose to a section being folded away, so they are
    the three things ``--full`` is not needed to see.
    """
    lines: list[str] = []

    facts: list[str] = []
    if result.changeset is not None:
        facts.append(f"source {result.changeset.origin}")
    if result.issue is not None:
        facts.append(f"against {result.issue.reference}")
    if facts:
        lines.append(f"_{' · '.join(facts)}_")

    if result.coverage:
        lines.append(
            f"**{len(result.coverage)} file(s) not fully reviewed:** "
            + _listed(
                f"`{item.path}` ({item.reason.value.replace('_', ' ')})" for item in result.coverage
            )
        )
    elif result.skipped_files:
        lines.append(
            f"**{len(result.skipped_files)} file(s) skipped (context budget):** "
            + _listed(f"`{path}`" for path in result.skipped_files)
        )

    if result.errors:
        lines.append("**Errors:**\n" + "\n".join(f"* {error}" for error in result.errors))

    if hid_sections:
        lines.append("_`--full` adds the agent prompts and the review info._")

    return "\n\n".join(lines)


FOOTER_LIST_LIMIT = 5
"""How many names the footer prints before it stops and counts the rest. The
review-info tree lists them all; a footer that did would stop being a footer."""


def _listed(names: Iterable[str]) -> str:
    shown = list(names)
    head = ", ".join(shown[:FOOTER_LIST_LIMIT])
    rest = len(shown) - FOOTER_LIST_LIMIT
    return f"{head} and {rest} more" if rest > 0 else head


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


MAX_WALKTHROUGH_MARKER = 8192
"""Ceiling on the carried overview, in characters of encoded payload.

The comment it rides in has a size limit of its own on every forge, and the
overview is already spelled out in prose above it -- so the copy is what gives
way when a very large review needs the room. Losing it costs one model call on
the next run, which is the cheaper half of the trade."""


MAX_WALKTHROUGH_PAYLOAD = 1 << 20
"""Ceiling on what a marker is allowed to inflate to, in bytes.

The token comes off a comment anyone can edit, and deflate lets a few kilobytes
of it stand for gigabytes once expanded. Stopping the decompressor at a bound no
honest overview comes near keeps a crafted marker from eating the process.
Anything larger is treated as unreadable, the same as any other bad marker."""


def encode_walkthrough(walkthrough: Walkthrough | None) -> str:
    """The overview as one marker-safe token, or ``""`` when it cannot ride along.

    Deflated first because the payload is prose, which compresses well and is
    competing for the same comment budget as the rendering of itself. Base64
    then keeps ``-->`` out of it, which a raw JSON overview could otherwise
    contain and close the comment early with.
    """
    if walkthrough is None:
        return ""
    raw = walkthrough.model_dump_json(exclude_defaults=True).encode("utf-8")
    token = base64.b64encode(zlib.compress(raw, 9)).decode("ascii")
    return token if len(token) <= MAX_WALKTHROUGH_MARKER else ""


def decode_walkthrough(token: str) -> Walkthrough | None:
    """The overview a published comment carries, or ``None`` if it carries none.

    Anything unreadable is treated as absent: a truncated marker, a payload from
    a schema that has since moved on, or someone else's text in the same shape.
    The caller then narrates the change again, which costs a model call but is
    never wrong.
    """
    if not token or len(token) > MAX_WALKTHROUGH_MARKER:
        return None
    try:
        packed = base64.b64decode(token.encode("ascii"), validate=True)
        pump = zlib.decompressobj()
        raw = pump.decompress(packed, MAX_WALKTHROUGH_PAYLOAD)
        if pump.unconsumed_tail:
            return None
        return Walkthrough.model_validate_json(raw)
    except (ValueError, zlib.error, ValidationError):
        return None
