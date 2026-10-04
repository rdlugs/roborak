"""The visible lifecycle of an explicitly published forge review."""

from __future__ import annotations

from dataclasses import dataclass

from roborak.core.models import ReviewResult
from roborak.core.verdict import Verdict, gate_for
from roborak.publish.base import _author, _viewer, _written_at
from roborak.render.markdown import LOGO_URL
from roborak.sources.base import SourceError
from roborak.sources.discussion import is_bot
from roborak.sources.forge import ForgeClient, Target

MARKER = "<!-- roborak:review-progress -->"

LOADING_GIF_URL = (
    "https://raw.githubusercontent.com/rdlugs/roborak/main/assets/roborak-reviewing-dark.gif"
)
"""Pinned to ``main`` and absolute for the same reasons as ``LOGO_URL``. Only the
transient body carries it, so ``finish`` replacing the body is what removes it."""


def _initial_body() -> str:
    return (
        "### 🔎 Review in progress\n\n"
        f'<img src="{LOADING_GIF_URL}" width="160" height="128" '
        'alt="Animated loading indicator: the roborak review is still running">\n\n'
        "roborak is reviewing this change. "
        "This comment will be updated when the review is complete.\n\n"
        f'<sub><img src="{LOGO_URL}" width="14" align="top"> <b>roborak</b></sub>'
        f"\n\n{MARKER}"
    )


@dataclass(frozen=True)
class ProgressRef:
    edit_path: str
    method: str


def _paths(target: Target) -> tuple[str, str, str]:
    if target.provider == "github":
        root = f"/repos/{target.project}/issues"
        return f"{root}/{target.number}/comments", f"{root}/comments", "PATCH"
    root = f"/projects/{target.encoded_project}/merge_requests/{target.number}/notes"
    return root, root, "PUT"


def start(target: Target, token: str) -> ProgressRef:
    """Reuse the newest owned progress note, including one from a completed run."""
    list_path, edit_root, method = _paths(target)
    with ForgeClient(target, token) as client:
        viewer = _viewer(client)
        candidates: list[tuple[object, int, ProgressRef]] = []
        for item in client.paginate(list_path):
            if not isinstance(item, dict) or MARKER not in str(item.get("body") or ""):
                continue
            identifier = item.get("id")
            if not isinstance(identifier, int):
                continue
            author = item.get("user") or item.get("author")
            if viewer:
                if _author(item).casefold() != viewer.casefold():
                    continue
            elif not is_bot(author, provider=target.provider):
                continue
            candidates.append(
                (
                    _written_at(item),
                    len(candidates),
                    ProgressRef(f"{edit_root}/{identifier}", method),
                )
            )

        if candidates:
            ref = max(candidates, key=lambda found: found[:2])[2]
            _edit(client, ref, _initial_body())
            return ref

        answer = client.post(list_path, {"body": _initial_body()})
        identifier = answer.get("id") if isinstance(answer, dict) else None
        if not isinstance(identifier, int):
            raise SourceError("The forge did not return an id for the review progress comment.")
        return ProgressRef(f"{edit_root}/{identifier}", method)


def finish(
    target: Target,
    token: str,
    ref: ProgressRef,
    result: ReviewResult | None,
    summary_url: str | None = None,
) -> None:
    """Replace the transient message with the outcome, even for a clean review."""
    if result is None:
        body = "roborak's review did not complete."
    else:
        gate = gate_for(result)
        if gate.verdict is Verdict.ERROR:
            body = "roborak's review did not complete."
        else:
            body = f"roborak's review is complete. {gate.summary_line()}"
            if summary_url:
                body += f"\n\n[Read the review summary]({summary_url})"
    with ForgeClient(target, token) as client:
        _edit(client, ref, f"{body}\n\n{MARKER}")


def _edit(client: ForgeClient, ref: ProgressRef, body: str) -> None:
    send = client.put if ref.method == "PUT" else client.patch
    send(ref.edit_path, {"body": body})
