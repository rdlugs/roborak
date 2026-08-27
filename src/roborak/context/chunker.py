"""Plan an oversized change across several model calls.

The compressor's job is to make a change fit by dropping things. This module's
job is to avoid dropping them: when a diff exceeds one context budget, review it
in several passes and merge the results, so a large pull request gets a complete
review rather than a partial one.

Files are classified by review role and grouped by direct relationships before
they are packed. Directory order remains available to the quality-eval harness
as the baseline the semantic planner replaces.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Literal

from roborak.core.models import (
    BoundaryKind,
    ChangedFile,
    ChangeSet,
    Hunk,
    ImpactMap,
    ReviewPlan,
    ReviewPlanFile,
    ReviewRole,
)

log = logging.getLogger(__name__)

MAX_CHUNKS = 8
"""Beyond this the review costs more than it is worth; the rest is omitted."""

MAX_CONTRACT_CONTEXTS = 12
MAX_CONTRACT_SUMMARY_CHARS = 320

ChunkStrategy = Literal["semantic", "directory"]


@dataclass(frozen=True)
class ContractContext:
    """A bounded reminder of a changed boundary, never primary diff content."""

    path: str
    name: str
    kind: str
    line: int
    summary: str = ""


@dataclass
class ChunkPlan:
    """Internal plan consumed by the reviewer and surfaced as coverage metadata."""

    chunks: list[ChangeSet]
    review: ReviewPlan
    contracts: list[ContractContext]


_ROLE_RANK: dict[ReviewRole, int] = {
    ReviewRole.CONTRACT: 0,
    ReviewRole.SCHEMA_CONFIG: 1,
    ReviewRole.IMPLEMENTATION: 2,
    ReviewRole.CONSUMER: 3,
    ReviewRole.TEST: 4,
    ReviewRole.LOW_SIGNAL: 5,
}

_TEST_PATH = re.compile(r"(^|/)(tests?|spec|__tests__)(/|$)|(^|/)test_|_test\.|\.spec\.|\.test\.")
_LOW_SIGNAL_PATH = re.compile(
    r"(^|/)(docs?|documentation|generated|vendor|dist|build|coverage)(/|$)"
)
_GENERATED_NAME = re.compile(
    r"(?:\.min\.(?:js|css)$|\.map$|"
    r"(?:^|/)(?:package-lock|yarn\.lock|pnpm-lock|uv\.lock)$)"
)
_SCHEMA_CONFIG_PATH = re.compile(
    r"(^|/)(migrations?|schemas?|config|deploy|deployment|k8s|terraform|\.github/workflows)(/|$)"
)
_SCHEMA_CONFIG_NAME = re.compile(
    r"(?:^|/)(?:dockerfile|compose(?:\.[^.]+)?\.ya?ml|pyproject\.toml|"
    r"(?:models?|schemas?|proto)\.[^/]+|"
    r"[^/]*config[^/]*\.(?:ya?ml|toml|json|ini)|[^/]*\.(?:sql|tf))$"
)
_PUBLIC_PATH = re.compile(
    r"(^|/)(api|public|routes?|entrypoints?|interfaces?|types?|protocols?)(/|$)"
)
_PUBLIC_NAME = re.compile(
    r"(?:^|/)(?:__init__|__main__|index|main|app|manage|cli|routes?|api|"
    r"types?|interfaces?|setup)\.[^/]+$"
)
_IMPORT = re.compile(
    r"(?:\bfrom\s+([\w.]+)\s+import|\bimport\s+([\w.]+)|"
    r"\brequire\(\s*['\"]([^'\"]+)['\"]|\bfrom\s+['\"]([^'\"]+)['\"]|"
    r"\buse\s+([\w:]+))"
)


def needs_chunking(
    changeset: ChangeSet,
    budget: int,
    count_tokens: Callable[[str], int],
    render: Callable[[ChangedFile], str],
) -> bool:
    return count_tokens(_joined(changeset.files, render)) > budget


def chunk(
    changeset: ChangeSet,
    budget: int,
    count_tokens: Callable[[str], int],
    render: Callable[[ChangedFile], str],
    *,
    impact: ImpactMap | None = None,
    strategy: ChunkStrategy = "semantic",
) -> list[ChangeSet]:
    """Compatibility wrapper returning only the planned changesets."""
    return plan_chunks(
        changeset,
        budget,
        count_tokens,
        render,
        impact=impact,
        strategy=strategy,
    ).chunks


def plan_chunks(
    changeset: ChangeSet,
    budget: int,
    count_tokens: Callable[[str], int],
    render: Callable[[ChangedFile], str],
    *,
    impact: ImpactMap | None = None,
    strategy: ChunkStrategy = "semantic",
) -> ChunkPlan:
    """Divide ``changeset`` into bounded, explainable review passes.

    Every chunk keeps the parent's metadata, so each pass sees the same title,
    description and forge refs and can produce properly anchored findings.
    """
    if not changeset.files:
        return ChunkPlan(chunks=[], review=ReviewPlan(), contracts=[])

    roles = _classify(changeset.files, impact)
    groups = (
        [[file] for file in sorted(changeset.files, key=_grouping_key)]
        if strategy == "directory"
        else _relationship_groups(changeset.files, roles, impact)
    )
    ordered_paths = [file.path for group in groups for file in group]
    chunks: list[list[ChangedFile]] = []
    current: list[ChangedFile] = []

    for group in groups:
        fragments = [
            fragment
            for file in group
            for fragment in _fragments(file, budget, count_tokens, render)
        ]
        if current and count_tokens(_joined([*current, *fragments], render)) > budget:
            chunks.append(current)
            current = []
        for file in fragments:
            if current and count_tokens(_joined([*current, file], render)) > budget:
                chunks.append(current)
                current = []
            current.append(file)

    if current:
        chunks.append(current)

    omitted: list[str] = []
    if len(chunks) > MAX_CHUNKS:
        for extra in chunks[MAX_CHUNKS:]:
            omitted.extend(f.path for f in extra if f.path not in omitted)
        chunks = chunks[:MAX_CHUNKS]
        log.warning(
            "change needs more than %d passes; %d file(s) omitted", MAX_CHUNKS, len(omitted)
        )

    pieces = [
        _sub_changeset(changeset, files, omitted if i == 0 else [])
        for i, files in enumerate(chunks)
    ]
    first_chunk = {
        file.path: index
        for index, files in enumerate(chunks, start=1)
        for file in files
        if file.path not in omitted
    }
    review = ReviewPlan(
        chunks=len(chunks),
        files=[
            ReviewPlanFile(
                path=path,
                role=roles[path],
                order=index,
                chunk=first_chunk.get(path),
                reviewed=path not in omitted,
            )
            for index, path in enumerate(ordered_paths, start=1)
        ],
    )
    log.debug("split %d files into %d semantic chunk(s)", len(changeset.files), len(chunks))
    return ChunkPlan(
        chunks=pieces,
        review=review,
        contracts=_contract_contexts(changeset.files, roles, impact),
    )


def _joined(files: list[ChangedFile], render: Callable[[ChangedFile], str]) -> str:
    """Exactly how the prompt template joins a chunk's files."""
    return "\n".join(render(f) for f in files)


def _sub_changeset(parent: ChangeSet, files: list[ChangedFile], omitted: list[str]) -> ChangeSet:
    return ChangeSet(
        files=files,
        title=parent.title,
        description=parent.description,
        base_sha=parent.base_sha,
        head_sha=parent.head_sha,
        base_ref=parent.base_ref,
        head_ref=parent.head_ref,
        origin=parent.origin,
        forge_ref=parent.forge_ref,
        discussions=parent.discussions,
        omitted_files=list(omitted),
    )


def _grouping_key(file: ChangedFile) -> tuple[str, str, str]:
    """The stable directory/language baseline retained for quality comparisons."""
    return (str(PurePosixPath(file.path).parent), file.language or "", file.path)


def _classify(files: list[ChangedFile], impact: ImpactMap | None) -> dict[str, ReviewRole]:
    """Assign every reviewable file one documented semantic role."""
    boundary_files = {node.file for node in impact.nodes} if impact is not None else set()
    externally_used = (
        {node.file for node in impact.nodes if node.consumers} if impact is not None else set()
    )
    roles: dict[str, ReviewRole] = {}
    for file in files:
        path = file.path.lower()
        text = _file_text(file)
        lowered = text.lower()
        if (
            _GENERATED_NAME.search(path)
            or "@generated" in lowered
            or "code generated" in lowered[:300]
        ):
            role = ReviewRole.LOW_SIGNAL
        elif _TEST_PATH.search(path):
            role = ReviewRole.TEST
        elif _SCHEMA_CONFIG_PATH.search(path) or _SCHEMA_CONFIG_NAME.search(path):
            role = ReviewRole.SCHEMA_CONFIG
        elif (
            file.path in externally_used
            or _PUBLIC_PATH.search(path)
            or _PUBLIC_NAME.search(path)
            or (file.path in boundary_files and _declares_public_surface(text))
        ):
            role = ReviewRole.CONTRACT
        elif _LOW_SIGNAL_PATH.search(path) or file.language in {"markdown", "rst", "text"}:
            role = ReviewRole.LOW_SIGNAL
        else:
            role = ReviewRole.IMPLEMENTATION
        roles[file.path] = role
    return roles


def _relationship_groups(
    files: list[ChangedFile], roles: dict[str, ReviewRole], impact: ImpactMap | None
) -> list[list[ChangedFile]]:
    """Build deterministic connected components from direct changed-file relationships."""
    by_path = {file.path: file for file in files}
    parent = {path: path for path in by_path}

    def find(path: str) -> str:
        while parent[path] != path:
            parent[path] = parent[parent[path]]
            path = parent[path]
        return path

    def join(left: str, right: str) -> None:
        a, b = find(left), find(right)
        if a != b:
            parent[max(a, b)] = min(a, b)

    nodes = impact.nodes if impact is not None else []
    terms_by_file: dict[str, set[str]] = {}
    for node in nodes:
        terms_by_file.setdefault(node.file, set()).add(node.name)

    texts = {file.path: _file_text(file) for file in files}
    for source, terms in terms_by_file.items():
        if source not in by_path:
            continue
        for target, text in texts.items():
            if target == source:
                continue
            if any(_mentions(text, term) for term in terms):
                join(source, target)
                if roles[target] is ReviewRole.IMPLEMENTATION:
                    roles[target] = ReviewRole.CONSUMER

    stems = {path: PurePosixPath(path).stem.removeprefix("test_") for path in by_path}
    for target, text in texts.items():
        imported = {part.replace(":", ".").split(".")[-1] for part in _imports(text)}
        for source, stem in stems.items():
            if source == target or not stem:
                continue
            same_test_subject = (
                roles[target] is ReviewRole.TEST
                and stem == stems[target]
                and roles[source] is not ReviewRole.TEST
            )
            if stem in imported or same_test_subject:
                join(source, target)
                if roles[target] is ReviewRole.IMPLEMENTATION:
                    roles[target] = ReviewRole.CONSUMER

    components: dict[str, list[ChangedFile]] = {}
    for file in files:
        components.setdefault(find(file.path), []).append(file)
    groups = [
        sorted(group, key=lambda file: (_ROLE_RANK[roles[file.path]], file.path))
        for group in components.values()
    ]
    return sorted(
        groups,
        key=lambda group: (
            min(_ROLE_RANK[roles[file.path]] for file in group),
            min(file.path for file in group),
        ),
    )


def _file_text(file: ChangedFile) -> str:
    if file.new_content is not None:
        return file.new_content
    return "\n".join(
        line[1:] if line.startswith(("+", "-", " ")) else line
        for hunk in file.hunks
        for line in hunk.content.splitlines()
        if not line.startswith("\\")
    )


def _declares_public_surface(text: str) -> bool:
    return bool(
        re.search(
            r"(?m)^\s*(?:export\s+|pub\s+|public\s+|class\s+[A-Z]|"
            r"(?:async\s+)?def\s+(?!_)[A-Za-z]|type\s+[A-Z]|interface\s+[A-Z])",
            text,
        )
    )


def _mentions(text: str, term: str) -> bool:
    return bool(term and re.search(rf"(?<![\w$]){re.escape(term)}(?![\w$])", text))


def _imports(text: str) -> set[str]:
    return {part for match in _IMPORT.finditer(text) for part in match.groups() if part}


def _contract_contexts(
    files: list[ChangedFile], roles: dict[str, ReviewRole], impact: ImpactMap | None
) -> list[ContractContext]:
    by_path = {file.path: file for file in files}
    contexts: list[ContractContext] = []
    seen: set[tuple[str, str]] = set()
    represented_paths: set[str] = set()
    if impact is not None:
        for node in impact.nodes:
            if node.file not in by_path or roles[node.file] not in {
                ReviewRole.CONTRACT,
                ReviewRole.SCHEMA_CONFIG,
            }:
                continue
            key = (node.file, node.name)
            if key in seen:
                continue
            seen.add(key)
            represented_paths.add(node.file)
            contexts.append(
                ContractContext(
                    path=node.file,
                    name=node.name,
                    kind=node.kind.value,
                    line=node.line,
                    summary=_changed_summary(by_path[node.file], node.line),
                )
            )
    for file in sorted(files, key=lambda item: item.path):
        if len(contexts) >= MAX_CONTRACT_CONTEXTS:
            break
        if roles[file.path] not in {ReviewRole.CONTRACT, ReviewRole.SCHEMA_CONFIG}:
            continue
        if file.path in represented_paths:
            continue
        key = (file.path, PurePosixPath(file.path).stem)
        if key in seen:
            continue
        contexts.append(
            ContractContext(
                path=file.path,
                name=PurePosixPath(file.path).stem,
                kind=(
                    BoundaryKind.CONFIG_KEY.value
                    if roles[file.path] is ReviewRole.SCHEMA_CONFIG
                    else BoundaryKind.EXPORT.value
                ),
                line=min(file.added_lines, default=1),
                summary=_changed_summary(file, min(file.added_lines, default=1)),
            )
        )
    return contexts[:MAX_CONTRACT_CONTEXTS]


def _changed_summary(file: ChangedFile, line: int) -> str:
    hunk = file.hunk_for_line(line) or next(iter(file.hunks), None)
    if hunk is None:
        return ""
    changed = [row for row in hunk.content.splitlines() if row.startswith(("+", "-"))][:6]
    return "\n".join(changed)[:MAX_CONTRACT_SUMMARY_CHARS]


def _fragments(
    file: ChangedFile,
    budget: int,
    count_tokens: Callable[[str], int],
    render: Callable[[ChangedFile], str],
) -> list[ChangedFile]:
    """Split a lone oversized file by hunk and then by line windows."""
    if count_tokens(render(file)) <= budget or not file.hunks:
        return [file]

    fragments: list[ChangedFile] = []
    for hunk in file.hunks:
        fragments.extend(_hunk_fragments(file, hunk, budget, count_tokens, render))
    return fragments or [file]


def _hunk_fragments(
    file: ChangedFile,
    hunk: Hunk,
    budget: int,
    count_tokens: Callable[[str], int],
    render: Callable[[ChangedFile], str],
) -> list[ChangedFile]:
    candidate = file.model_copy(update={"hunks": [hunk]})
    lines = hunk.content.splitlines()
    if count_tokens(render(candidate)) <= budget or len(lines) <= 1:
        return [candidate]

    midpoint = len(lines) // 2
    overlap = min(3, max(0, len(lines) // 4))
    left = _slice_hunk(hunk, 0, midpoint + overlap)
    right = _slice_hunk(hunk, midpoint - overlap, len(lines))
    return [
        *_hunk_fragments(file, left, budget, count_tokens, render),
        *_hunk_fragments(file, right, budget, count_tokens, render),
    ]


def _slice_hunk(hunk: Hunk, start: int, end: int) -> Hunk:
    lines = hunk.content.splitlines()
    old_line, new_line = hunk.old_start, hunk.new_start
    for line in lines[:start]:
        if line.startswith("\\"):
            continue
        if not line.startswith("+"):
            old_line += 1
        if not line.startswith("-"):
            new_line += 1

    selected = lines[start:end]
    old_count = sum(1 for line in selected if not line.startswith(("+", "\\")))
    new_count = sum(1 for line in selected if not line.startswith(("-", "\\")))
    new_end = new_line + new_count
    return Hunk(
        old_start=old_line,
        old_lines=old_count,
        new_start=new_line,
        new_lines=new_count,
        header=f"@@ -{old_line},{old_count} +{new_line},{new_count} @@",
        content="\n".join(selected),
        added_lines={line for line in hunk.added_lines if new_line <= line < new_end},
        line_map={
            line: position for line, position in hunk.line_map.items() if new_line <= line < new_end
        },
    )
