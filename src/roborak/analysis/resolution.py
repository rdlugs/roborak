"""Whether the commits after a published finding actually fixed it.

The stage that lets roborak close its own threads. It sits beside the
investigation pass and asks the mirror-image question: that one settles a
candidate nobody has seen yet, this one revisits a comment a reviewer has already
read and may have acted on.

The two mistakes available here are not symmetrical, and every decision below
follows from that. A thread left open costs a maintainer one click. A thread
closed on a guess buries a real finding under a claim that it was checked, in the
one place a reader would have looked for it. So ``inconclusive`` is the default
and the destination of every failure -- an untrusted checkout, a revision this
clone does not have, a provider error, an unreadable reply, a thread the model
declined to name. Nothing here ever *creates* a finding; the worst it can do is
decline to retire one.

Evidence is gathered in two halves. The commit range and its diff are computed by
roborak before any model call, because the revisions bounding that range are
roborak's to choose and a model that could pick them could pick a range that
flattered the answer. What the model may then ask for is the current state of the
tree, through the same read-only boundary the investigation pass uses.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from roborak.core.config import InvestigateConfig
from roborak.core.models import ChangeSet, FixVerdict
from roborak.investigate import availability, tools
from roborak.llm.parser import (
    RESOLUTION_TOOLS,
    ParseError,
    parse_investigation_requests,
    parse_resolution_verdicts,
)
from roborak.llm.prompt import build_resolution_prompt
from roborak.publish.threads import OpenThread

log = logging.getLogger(__name__)

Complete = Callable[[str, str], str]

MAX_HISTORY_CHARS = 6000
"""How much of one file's post-comment history reaches the prompt.

Generous next to a tool result, because this is the evidence rather than a lookup:
a fix the model cannot see is a fix it will rightly call inconclusive."""


def verify_fixes(
    threads: list[OpenThread],
    changeset: ChangeSet,
    *,
    repo: Path,
    config: InvestigateConfig,
    complete: Complete,
    fallback_base: str = "",
) -> list[FixVerdict]:
    """One verdict per thread, in the order the threads arrived.

    ``fallback_base`` is the head an earlier run recorded locally, used for a
    thread whose own anchor the forge did not report. It is genuinely a fallback:
    the anchor travels with the thread and is therefore right on a machine that
    has never published this change, which local state never is.
    """
    verdicts = {thread.key: FixVerdict(thread=thread.key) for thread in threads}
    if not threads:
        return []

    head = changeset.head_sha
    if not head:
        log.debug("no head revision to compare against; leaving every thread open")
        return list(verdicts.values())

    access = availability.resolve(changeset, repo)
    if not (access.reads_working_tree and access.searches):
        # The same bar the investigation stage sets, and for a stronger reason: a
        # tree that is not the reviewed head is not the end of the commit range,
        # so every answer read out of it would be about different code.
        log.info("the checkout is not the reviewed change; leaving every thread open")
        return list(verdicts.values())

    evidence = _history(threads, repo=repo, head=head, config=config, fallback=fallback_base)
    candidates = [thread for thread in threads if thread.key in evidence][: config.max_candidates]
    if not candidates:
        return list(verdicts.values())

    ids = {f"t{index + 1}": thread for index, thread in enumerate(candidates)}
    decided = _ask(ids, evidence, repo=repo, config=config, complete=complete)

    for key, thread in ids.items():
        if (answer := decided.get(key)) is not None:
            verdicts[thread.key] = FixVerdict.model_validate({"thread": thread.key, **answer})
    return list(verdicts.values())


def _history(
    threads: list[OpenThread],
    *,
    repo: Path,
    head: str,
    config: InvestigateConfig,
    fallback: str,
) -> dict[str, tuple[list[tuple[str, str]], str]]:
    """The commits after each thread's anchor, for the threads that have any.

    A thread missing from the result is one no model call can help with, and the
    three ways to be missing are all "we could not tell": no usable anchor, an
    anchor this clone never fetched -- routine on the shallow checkout CI hands
    out -- or a range in which nothing went near the file at all. Only the last
    of those is evidence, and it is evidence of no fix rather than of a fix.
    """
    found: dict[str, tuple[list[tuple[str, str]], str]] = {}
    for thread in threads:
        base = thread.anchor_sha or fallback
        if not base or base == head:
            continue
        if not tools.has_revision(repo, base, config=config):
            log.debug("this checkout does not have %s; leaving %s open", base[:8], thread.location)
            continue
        commits = tools.commits_touching(repo, thread.file, base=base, head=head, config=config)
        if not commits:
            continue
        shown = tools.show_commits(repo, thread.file, base=base, head=head, config=config)
        if not shown.ok:
            continue
        found[thread.key] = (commits, shown.text[:MAX_HISTORY_CHARS])
    return found


def _ask(
    ids: dict[str, OpenThread],
    evidence: dict[str, tuple[list[tuple[str, str]], str]],
    *,
    repo: Path,
    config: InvestigateConfig,
    complete: Complete,
) -> dict[str, dict[str, Any]]:
    """The round loop: look, then decide. Every exit that is not a verdict is silence."""
    known = {key: {sha for sha, _ in evidence[thread.key][0]} for key, thread in ids.items()}
    operations: list[dict[str, Any]] = []
    opened: set[str] = set()

    for round_index in range(1, config.max_rounds + 1):
        final = round_index == config.max_rounds or len(opened) >= config.max_files
        prompt = build_resolution_prompt(
            threads=[_describe(key, thread, evidence) for key, thread in ids.items()],
            operations=operations,
            limits={
                "max_requests_per_round": config.max_requests_per_round,
                "max_lines_per_read": config.max_lines_per_read,
                "max_search_results": config.max_search_results,
                "rounds_remaining": config.max_rounds - round_index,
            },
            can_search=True,
            final_round=final,
        )
        try:
            reply = complete(prompt.system, prompt.user)
        except Exception as exc:  # noqa: BLE001 - a failed stage never fails the review
            log.warning("resolution round %d failed: %s", round_index, exc)
            return {}

        try:
            verdicts = parse_resolution_verdicts(reply, valid_ids=set(ids), known_commits=known)
        except ParseError as exc:
            log.debug("unreadable resolution reply: %s", exc)
            return {}
        if verdicts:
            return verdicts
        if final:
            return {}

        try:
            requests = parse_investigation_requests(
                reply, limit=config.max_requests_per_round, tools=RESOLUTION_TOOLS
            )
        except ParseError:
            requests = []
        if not requests:
            return {}

        for request in requests:
            operations.append(_execute(request, repo, config, opened))
    return {}


def _describe(
    key: str,
    thread: OpenThread,
    evidence: dict[str, tuple[list[tuple[str, str]], str]],
) -> dict[str, Any]:
    """One thread as the prompt sees it: what was said, and what happened since.

    Keyed by an id roborak issued rather than by the forge's own handle, for the
    same reason the investigation stage does it: an opaque id is one less thing a
    reply can rename itself into.
    """
    commits, changes = evidence[thread.key]
    return {
        "id": key,
        "file": thread.file,
        "line": thread.line,
        "finding": thread.body,
        "commits": [{"sha": sha[:12], "subject": subject} for sha, subject in commits],
        "changes_since": changes,
    }


def _execute(
    request: dict[str, str],
    repo: Path,
    config: InvestigateConfig,
    opened: set[str],
) -> dict[str, Any]:
    """Run one request through the read-only boundary and record what it produced."""
    tool = request.get("tool", "")
    path = request.get("path", "")

    if tool == "read_file":
        if path not in opened and len(opened) >= config.max_files:
            result = tools.ToolResult(error="the file budget for this pass is spent")
        else:
            opened.add(path)
            result = tools.read_lines(
                repo,
                path,
                start=_as_line(request.get("start"), 1),
                end=_as_line(request.get("end"), config.max_lines_per_read),
                config=config,
            )
    elif tool == "search":
        result = tools.search(
            repo,
            request.get("pattern", ""),
            regex=request.get("regex", "") == "true",
            path_prefix=path,
            config=config,
        )
    else:
        result = tools.ToolResult(error=f"unavailable in this pass: {tool}")

    arguments = ", ".join(f"{k}={v}" for k, v in sorted(request.items()) if k != "tool")
    return {
        "request": f"{tool}({arguments})",
        "truncated": result.truncated,
        "result": result.text or result.error,
    }


def _as_line(value: str | None, default: int) -> int:
    """A line number out of a model's reply, or the default it did not give."""
    try:
        return max(1, int(str(value)))
    except (TypeError, ValueError):
        return default
