"""Expand a hunk to the symbol that contains it.

A diff hunk is a window with arbitrary edges: it routinely starts mid-function and
ends mid-condition. A model shown such a fragment has to guess at the surrounding
control flow, and guesses are what false positives are made of. Here we use
tree-sitter to find the enclosing function or class and report its bounds, so the
prompt can carry a complete symbol instead.

tree-sitter is an optional dependency. Everything here degrades to "no extra
context" when it is missing, so a review still runs.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from roborak.core.models import ChangedFile, Hunk

log = logging.getLogger(__name__)

_GRAMMAR = {
    "csharp": "csharp",
    "cpp": "cpp",
    "shell": "bash",
    "vue": "vue",
    "terraform": "terraform",
}

_SYMBOL_TYPES = frozenset(
    {
        "arrow_function",
        "class",
        "class_declaration",
        "class_definition",
        "class_specifier",
        "constructor_declaration",
        "decorated_definition",
        "enum_item",
        "function_declaration",
        "function_definition",
        "function_expression",
        "function_item",
        "generator_function_declaration",
        "impl_item",
        "interface_declaration",
        "method",
        "method_declaration",
        "method_definition",
        "module",
        "object_declaration",
        "struct_item",
        "struct_specifier",
        "trait_item",
        "type_declaration",
    }
)

MAX_SYMBOL_LINES = 200
"""A symbol larger than this is not worth pulling in whole; the hunk is enough."""


@dataclass
class SymbolSpan:
    """The extent of the symbol containing a hunk, in 1-based new-file lines."""

    name: str
    kind: str
    start_line: int
    end_line: int

    @property
    def line_count(self) -> int:
        return self.end_line - self.start_line + 1


def available() -> bool:
    return _load_parser("python") is not None


@lru_cache(maxsize=32)
def _load_parser(language: str) -> Any | None:
    """Load a grammar once per language, or return None if unavailable."""
    try:
        from tree_sitter_language_pack import get_parser
    except ImportError:
        log.debug("tree-sitter not installed; AST context is disabled")
        return None

    try:
        return get_parser(_GRAMMAR.get(language, language))
    except Exception:  # noqa: BLE001 - a grammar we do not ship is not an error
        log.debug("no tree-sitter grammar for %s", language)
        return None


def enclosing_symbol(file: ChangedFile, hunk: Hunk) -> SymbolSpan | None:
    """Find the smallest named symbol fully containing ``hunk``."""
    if file.language is None or file.new_content is None:
        return None
    parser = _load_parser(file.language)
    if parser is None:
        return None

    try:
        tree = parser.parse(file.new_content.encode("utf-8"))
    except Exception:  # noqa: BLE001 - a broken parse is normal mid-review
        log.debug("could not parse %s", file.path)
        return None

    target_start = hunk.new_start - 1
    target_end = hunk.new_end - 1

    best: SymbolSpan | None = None
    root = tree.root_node
    for node in _walk(root):
        if node is root or node.type not in _SYMBOL_TYPES:
            continue
        if node.start_point[0] > target_start or node.end_point[0] < target_end:
            continue
        span = SymbolSpan(
            name=_node_name(node),
            kind=node.type,
            start_line=node.start_point[0] + 1,
            end_line=node.end_point[0] + 1,
        )
        if best is None or span.line_count < best.line_count:
            best = span

    if best is not None and best.line_count > MAX_SYMBOL_LINES:
        log.debug(
            "symbol %s in %s is %d lines; too large to inline",
            best.name,
            file.path,
            best.line_count,
        )
        return None
    return best


def _walk(node: Any) -> Any:
    yield node
    for child in node.children:
        yield from _walk(child)


def _node_name(node: Any) -> str:
    """Best-effort symbol name, across grammars that disagree about field names."""
    for field in ("name", "declarator"):
        child = node.child_by_field_name(field)
        if child is not None:
            text = child.text
            if isinstance(text, bytes):
                return text.decode("utf-8", "replace").split("(")[0].strip()
    return "<anonymous>"


def symbol_context(file: ChangedFile, hunk: Hunk) -> str:
    """A one-line note naming the symbol the hunk sits in, for the prompt.

    Deliberately a note rather than the symbol's full text: naming the enclosing
    function costs a handful of tokens and removes most of the ambiguity, whereas
    inlining the whole body would blow the budget on code the model can already
    partly see.
    """
    span = enclosing_symbol(file, hunk)
    if span is None:
        return ""
    kind = span.kind.replace("_definition", "").replace("_declaration", "").replace("_", " ")
    return f"within {kind} `{span.name}` (lines {span.start_line}-{span.end_line})"
