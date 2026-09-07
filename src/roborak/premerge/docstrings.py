"""How much of what this change touched carries documentation.

Scoped to the symbols the diff actually touched, not to whole files. A one-line
fix inside a legacy module did not make that module undocumented, and a check
that says otherwise trains its reader to ignore it -- the same reason the static
pass drops findings off changed lines.

Every language ``tree_sitter_language_pack`` parses is measured, through two
arms rather than a per-language table. Only Python links a docstring to its
symbol structurally, so the leading-string arm answers for Python alone -- a
string opening a body anywhere else is a plain expression, ``"use strict"``
included. godoc, JSDoc and Javadoc are comments attached by proximity, which
tree-sitter does not connect to the node they describe, so the second arm
below is a heuristic and will miscount for languages with weak conventions. That
is the deliberate trade: the check defaults to ``warning``, and reporting a whole
language as unmeasurable would tell a reader less than measuring it and saying so.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from roborak.context.ast_context import SYMBOL_TYPES, node_name, parse, walk
from roborak.context.impact import content_at_head
from roborak.core.models import ChangedFile, ChangeSet


@dataclass(frozen=True)
class SymbolCoverage:
    """One symbol the diff touched, and whether it is documented."""

    path: str
    name: str
    kind: str
    line: int
    documented: bool


@dataclass
class CoverageMeasurement:
    """What the pass could see, and what it deliberately could not."""

    symbols: list[SymbolCoverage]
    unparsed_files: list[str]
    """Changed files a grammar could not read. Excluded from the denominator
    rather than counted as undocumented: we did not look, so we cannot say."""

    unreadable_files: list[str] = field(default_factory=list)
    """Changed files whose new text could not be obtained at all.

    Excluded for the same reason and reported separately, because they are a
    different fact. Blaming a missing grammar for a file nobody managed to read
    sends the reader to look for a tree-sitter package that would not have helped.
    """

    parsed_any: bool = False
    """Whether any eligible file was read and parsed, symbols or not.

    A file the grammar read perfectly well can still yield no touched symbol, and
    that is not the same fact as a file nobody could read. The no-ratio summary
    needs this to tell "nothing was readable" from "nothing was touched".
    """

    @property
    def documented(self) -> int:
        return sum(1 for symbol in self.symbols if symbol.documented)

    @property
    def total(self) -> int:
        return len(self.symbols)

    @property
    def ratio(self) -> float | None:
        """``None`` when there was nothing to measure, never a hollow 1.0."""
        return self.documented / self.total if self.symbols else None

    @property
    def undocumented(self) -> list[SymbolCoverage]:
        return [symbol for symbol in self.symbols if not symbol.documented]


def measure(changeset: ChangeSet, repo: Path | None = None) -> CoverageMeasurement:
    """Documentation coverage over the symbols this change touched.

    ``repo`` is the tree the change lives in, and only the forge sources need it:
    they carry hunks alone, so without it every pull request measured nothing and
    reported itself unparseable. The content is read by sha out of the reviewed
    commit and used for the parse only -- writing it back onto the changeset would
    put whole files into the compressor's budget and the anchoring path.
    """
    symbols: list[SymbolCoverage] = []
    unparsed: list[str] = []
    unreadable: list[str] = []
    parsed_any = False
    for file in changeset.files:
        if file.change_type == "deleted" or file.is_binary or not file.hunks:
            continue
        content = _content(file, repo, changeset.head_sha)
        if content is None:
            unreadable.append(file.path)
            continue
        tree = parse(file.language, content)
        if tree is None:
            unparsed.append(file.path)
            continue
        parsed_any = True
        symbols.extend(_symbols_for(file, tree))
    return CoverageMeasurement(
        symbols=symbols,
        unparsed_files=unparsed,
        unreadable_files=unreadable,
        parsed_any=parsed_any,
    )


def _content(file: ChangedFile, repo: Path | None, head: str) -> str | None:
    """The file's new text, from the change itself or from the reviewed commit."""
    if file.new_content is not None:
        return file.new_content
    if repo is None:
        return None
    return content_at_head(file, repo, head)


def _symbols_for(file: ChangedFile, tree: Any) -> list[SymbolCoverage]:
    """The smallest named symbol containing each added line, deduplicated by position.

    Line by line rather than hunk by hunk: a hunk carries unchanged context and can
    span the tail of one function and the head of the next, so its full range is
    contained by neither, and resolving it at once would credit the file with no
    touched symbol at all -- or with an enclosing one the change never wrote.
    Deletion-only hunks contribute nothing: they add no new-file line, and the
    symbol that survives around a deletion is not a symbol this change documented.
    """
    found: dict[tuple[int, int], Any] = {}
    for hunk in file.hunks:
        for lineno in sorted(hunk.added_lines):
            node = _smallest_containing(tree.root_node, lineno - 1, lineno - 1)
            if node is not None:
                found[(node.start_point[0], node.end_point[0])] = node
    return [
        SymbolCoverage(
            path=file.path,
            name=node_name(node),
            kind=node.type,
            line=node.start_point[0] + 1,
            documented=_is_documented(node, file.language),
        )
        for node in found.values()
    ]


def _smallest_containing(root: Any, start_row: int, end_row: int) -> Any | None:
    best: Any | None = None
    for node in walk(root):
        if node is root or node.type not in SYMBOL_TYPES:
            continue
        if node.start_point[0] > start_row or node.end_point[0] < end_row:
            continue
        if best is None or _span(node) < _span(best):
            best = node
    return best


def _span(node: Any) -> int:
    return node.end_point[0] - node.start_point[0]


_DOCSTRING_LANGUAGES = frozenset({"python"})
"""Languages where a string opening a body *is* the symbol's documentation.

Everywhere else a leading string is an ordinary expression -- JavaScript's
``"use strict"`` is a directive, not an API description -- and reading it as a
docstring credits coverage that was never written."""


def _is_documented(node: Any, language: str | None) -> bool:
    """A leading string in the body, or a comment on the line just above."""
    if language in _DOCSTRING_LANGUAGES and _has_leading_string(node):
        return True
    return _has_preceding_comment(node)


_BODY_TYPES = frozenset({"block", "statement_block", "class_body", "declaration_list"})
"""What a symbol's body is called when the grammar does not name it as a field."""

_BODY_OPENERS = frozenset({"{", ":", "comment", "line_comment", "block_comment"})
"""Tokens that can precede the first real statement without being one."""


def _has_leading_string(node: Any) -> bool:
    """The Python form: the symbol's body opens with a string literal."""
    body = node.child_by_field_name("body")
    if body is None:
        body = next((child for child in node.children if child.type in _BODY_TYPES), None)
    if body is None:
        return False
    for child in body.children:
        if child.type in _BODY_OPENERS:
            continue
        if child.type == "expression_statement" and child.children:
            return child.children[0].type == "string"
        return child.type == "string"
    return False


def _has_preceding_comment(node: Any) -> bool:
    """The godoc/JSDoc form: a comment ending on the line directly above.

    Walks up through wrappers such as ``decorated_definition`` and export
    statements first, so a decorated or exported symbol is judged by what sits
    above the decoration rather than by the decoration itself.
    """
    top = node
    while top.parent is not None and top.parent.start_point[0] == top.start_point[0]:
        top = top.parent
    previous = top.prev_sibling
    while previous is not None and previous.type in {"decorator", "modifier"}:
        previous = previous.prev_sibling
    if previous is None or "comment" not in previous.type:
        return False
    if previous.start_point[0] >= top.start_point[0]:
        return False  # An inline comment beside the symbol, not one above it.
    # Grammars disagree about whether a line comment ends on its own row or rolls
    # onto the next, so both readings of "directly above" count. A gap of two or
    # more is a blank line, which is a comment about something else.
    if top.start_point[0] - previous.end_point[0] > 1:
        return False
    text = previous.text
    body = text.decode("utf-8", "replace") if isinstance(text, bytes) else str(text)
    return bool(body.strip(" \t/*#-").strip())
