"""What a change reaches, beyond the lines it touched.

A diff is a keyhole. Reviewing one tells you whether the code inside it is
self-consistent, and says nothing at all about the unchanged caller two
directories away that the new signature has just broken. ``ast_context`` looks
*inward* from a hunk to the symbol containing it; this looks *outward*, from the
symbols and contracts a change touched to the code that depends on them.

Three things make that safe to feed to a model:

* **Consumers are evidence, never surface.** They are shown to the model and are
  never added to the changeset, so ``parser.parse_findings`` and
  ``validator.anchor_to_changed_lines`` already make them ineligible for a
  comment. A contract break is anchored to the changed line responsible for it.
* **Absence is not containment.** Text search cannot see an alias, a re-export or
  a name assembled at runtime, so a search that found nothing says exactly that.
  Only a symbol a parser identified, searched completely, is allowed to report
  ``contained`` -- and even then the note names what remains possible.
* **Everything is bounded, and says when it was.** Nodes, consumers per node,
  files walked, snippet lines, wall clock and prompt tokens all have ceilings.
  Any of them biting sets ``truncated`` and writes down which.

Nothing here raises. Every failure is a status on the map, because a review that
falls over because the optional pass could not run is worse than one without it.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from roborak.context import ast_context, forge_checkout
from roborak.context.diff import detect_language
from roborak.core.config import ImpactConfig
from roborak.core.models import (
    BoundaryKind,
    ChangedFile,
    ChangeSet,
    Consumer,
    ConsumerRelation,
    ImpactMap,
    ImpactNode,
    ImpactStatus,
    Verification,
)
from roborak.render.snippet import window

# The one list of directories nobody wants read, shared with the paths source so
# a walk started here and a walk started there cannot disagree about node_modules.
from roborak.sources.paths import SKIP_DIRS

log = logging.getLogger(__name__)

IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")

MIN_TERM_LENGTH = 3
"""Shorter than this and a name is not a name, it is a substring of the repository.

``id``, ``on`` and ``x`` match everything and mean nothing; the consumer cap would
fill with noise and push out a real reference."""

MAX_FILE_BYTES = 512 * 1024
"""Ceiling on a file the walk fallback will read, matching the paths source."""

TOO_GENERIC = frozenset(
    {
        "args",
        "body",
        "code",
        "config",
        "count",
        "data",
        "date",
        "end",
        "error",
        "file",
        "id",
        "index",
        "item",
        "items",
        "key",
        "kwargs",
        "line",
        "list",
        "message",
        "name",
        "path",
        "result",
        "size",
        "start",
        "status",
        "text",
        "time",
        "title",
        "type",
        "url",
        "user",
        "value",
        "version",
    }
)
"""Words too common to be evidence of anything, for boundaries found by pattern.

A parser that says ``run`` is a function has identified a real declaration, and
tracing it is worth doing. A regular expression that guesses ``path`` is a
configuration key has found a word, and its "consumers" will be every file in the
repository. The stop list applies only to the guessing half."""


_PRIORITY: dict[BoundaryKind, int] = {
    BoundaryKind.SYMBOL: 0,
    BoundaryKind.EXPORT: 1,
    BoundaryKind.ROUTE: 2,
    BoundaryKind.EVENT: 2,
    BoundaryKind.SCHEMA_FIELD: 3,
    BoundaryKind.ENV_VAR: 3,
    BoundaryKind.CONFIG_KEY: 4,
}
"""Which boundaries are worth the node budget first.

A parser-identified symbol is a fact; a configuration key matched by a colon is a
guess. When ``max_nodes`` bites, the facts should be what survives."""


@dataclass(frozen=True)
class _Contract:
    """One way a non-symbol contract is spelled, and what kind of contract it is."""

    kind: BoundaryKind
    pattern: re.Pattern[str]
    languages: frozenset[str] | None = None
    """``None`` matches any language; otherwise only these."""


_CONTRACTS: tuple[_Contract, ...] = (
    _Contract(
        BoundaryKind.ENV_VAR,
        re.compile(r"""os\.getenv\(\s*["']([A-Za-z_][A-Za-z0-9_]*)["']"""),
    ),
    _Contract(
        BoundaryKind.ENV_VAR,
        re.compile(r"""os\.environ(?:\.get)?[(\[]\s*["']([A-Za-z_][A-Za-z0-9_]*)["']"""),
    ),
    _Contract(BoundaryKind.ENV_VAR, re.compile(r"process\.env\.([A-Za-z_$][\w$]*)")),
    _Contract(
        BoundaryKind.ENV_VAR,
        re.compile(r"""(?<!\.)\bgetenv\(\s*["']([A-Za-z_][A-Za-z0-9_]*)["']"""),
    ),
    _Contract(
        BoundaryKind.ROUTE,
        re.compile(r"""@\s*\w+\.(?:route|get|post|put|patch|delete)\(\s*["']([^"']+)["']"""),
    ),
    _Contract(
        BoundaryKind.ROUTE,
        re.compile(
            r"""\b(?:app|router|api)\.(?:get|post|put|patch|delete|use)\(\s*["']([^"']+)["']"""
        ),
    ),
    _Contract(BoundaryKind.ROUTE, re.compile(r"""Route::\w+\(\s*["']([^"']+)["']""")),
    _Contract(
        BoundaryKind.EVENT,
        re.compile(r"""\.(?:emit|publish|dispatch|subscribe|listen)\(\s*["']([\w.:/-]+)["']"""),
    ),
    _Contract(
        BoundaryKind.CONFIG_KEY,
        re.compile(r"""^\s*["']?([A-Za-z_][\w.-]*)["']?\s*[:=]\s*\S"""),
        frozenset({"yaml", "toml", "json", "ini"}),
    ),
    _Contract(
        BoundaryKind.SCHEMA_FIELD,
        re.compile(r"^\s*(?:ADD\s+COLUMN|DROP\s+COLUMN|ALTER\s+COLUMN)\s+[\"'`]?(\w+)", re.I),
        frozenset({"sql"}),
    ),
)

SCHEMA_BASES = frozenset({"BaseModel", "Schema", "TypedDict", "Model", "Base", "Document"})
"""Superclasses that make a class a declared schema rather than an implementation.

A field on one of these is a contract with everything that serialises, validates
or persists through it -- which is exactly the sort of consumer a diff hides."""

_CLASS_NODES = frozenset({"class_definition", "class_declaration"})
_FIELD_NODES = frozenset({"typed_parameter", "assignment", "field_declaration"})

_EXPORTS: tuple[tuple[frozenset[str], re.Pattern[str]], ...] = (
    (
        frozenset({"python"}),
        re.compile(r"^([A-Z][A-Z0-9_]{2,})\s*(?::[^=]+)?=\s*\S"),
    ),
    (
        frozenset({"javascript", "typescript", "vue"}),
        re.compile(
            r"^\s*export\s+(?:default\s+)?"
            r"(?:const|let|var|function|class|interface|type|enum)\s+([A-Za-z_$][\w$]*)"
        ),
    ),
)

_IMPORT_LINE = re.compile(
    r"^\s*(?:from\s+\S+\s+import\b|import\b|export\b|use\s|#include\b|require\s*\()"
)
_TEST_PATH = re.compile(r"(^|/)(tests?|spec|__tests__)(/|$)|(^|/)test_|_test\.|\.spec\.|\.test\.")
_CONFIG_LANGUAGES = frozenset({"yaml", "toml", "json", "ini"})

PROSE_LANGUAGES = frozenset({"markdown", "rst", "text"})
"""Documentation is not a consumer.

A changelog entry that mentions ``parse`` does not call it, and a README that
names a function does not break when its signature changes. Left in, prose is
where most of a common name's matches come from, and it crowds out the caller
that actually matters."""

_LITERAL_NODES = frozenset(
    {
        "comment",
        "line_comment",
        "block_comment",
        "string",
        "string_content",
        "string_literal",
        "raw_string_literal",
        "char_literal",
        "template_string",
        "interpreted_string_literal",
    }
)
"""Node types a name can appear inside without being a reference to anything."""


def analyse(
    changeset: ChangeSet, repo: Path, config: ImpactConfig, *, forge_token: str | None = None
) -> ImpactMap:
    """Map the blast radius of ``changeset``, as far as the evidence allows.

    A forge change whose head commit is nowhere local used to end here: no tree,
    no consumers, nothing to say. ``forge_checkout`` fetches a throwaway one, so
    the search runs against exactly the reviewed commit. Its lifetime is this
    call -- every path below reads the tree while the map is being built, and
    none of them holds a reference to it afterwards.
    """
    present = _head_present(changeset, repo)
    with forge_checkout.acquire(
        changeset, repo, config, head_present=present, token=forge_token
    ) as fetched:
        return _analyse(changeset, fetched.repo or repo, config, fetched=fetched, present=present)


def _analyse(
    changeset: ChangeSet,
    repo: Path,
    config: ImpactConfig,
    *,
    fetched: forge_checkout.Checkout,
    present: bool,
) -> ImpactMap:
    """The map itself, against whichever tree ``analyse`` settled on."""
    status, notes = _availability(changeset, fetched=fetched, present=present)
    if status is not None:
        return ImpactMap(status=status, notes=notes)

    # A verified temporary checkout *is* the change under review, so the caveat
    # that the tree may not match would be false. Only the local-checkout path,
    # where a matching commit says nothing about the working directory, keeps it.
    limited = changeset.origin in {"gitlab", "github"} and not fetched.verified
    nodes, parsed_any = _seed(changeset, repo, config)
    if not nodes:
        return ImpactMap(
            status=ImpactStatus.UNSUPPORTED,
            notes=[
                *notes,
                "No changed symbol or contract could be identified: no tree-sitter grammar "
                "covers these files, or this change touches no traceable boundary.",
            ],
        )

    truncated = len(nodes) > config.max_nodes
    if truncated:
        notes.append(
            f"Traced the first {config.max_nodes} of {len(nodes)} changed boundaries "
            f"(`impact.max_nodes`)."
        )
        nodes = nodes[: config.max_nodes]

    if not parsed_any:
        notes.append(
            "No parser was available for the changed files, so boundaries were "
            "identified by pattern alone and containment cannot be claimed."
        )

    search = _Search(repo=repo, config=config, changed={file.path for file in changeset.files})
    hits = search.find([node.name for node in nodes])
    for node in nodes:
        _resolve(node, hits.get(node.name, []), search)

    if search.method == "walk":
        notes.append(
            "`git grep` was unavailable, so references were found by walking the "
            "directory. Dependency and build directories, oversized files and "
            "anything unreadable were not searched, and `.gitignore` was not consulted."
        )
    if search.truncated:
        truncated = True
        limit = (
            f"{config.timeout_seconds}s (`impact.timeout_seconds`)"
            if search.timed_out
            else f"{config.max_files_scanned} files (`impact.max_files_scanned`)"
        )
        notes.append(f"The search stopped after {limit}; containment cannot be claimed.")
    truncated = _fit_budget(nodes, config, notes) or truncated

    if limited:
        notes.insert(
            0,
            "The change was fetched from the forge and searched against the local "
            "checkout, which may not hold exactly the code under review.",
        )
    elif fetched.verified:
        notes.insert(0, fetched.notes[0])

    return ImpactMap(
        nodes=nodes,
        status=_map_status(nodes, limited=limited),
        method=search.method,
        truncated=truncated,
        notes=notes,
    )


def _availability(
    changeset: ChangeSet, *, fetched: forge_checkout.Checkout, present: bool
) -> tuple[ImpactStatus | None, list[str]]:
    """Whether there is anything to search, decided before any work happens.

    ``None`` means go ahead. The interesting case is ``paths``: a directory with
    no repository is reviewed whole file by file, so every file is already in the
    changeset and every line counts as added. There is no *unchanged* consumer to
    find -- the whole tree is the review surface, and the model has already been
    shown it. Searching would spend the budget to produce an empty map, and an
    empty map reads far too much like "this change is contained".
    """
    if changeset.origin == "paths":
        note = (
            "Every file in the directory is under review, so there is no unchanged consumer to map."
        )
        if changeset.omitted_files:
            note += (
                f" {len(changeset.omitted_files)} file(s) the walk left out were not "
                f"searched either."
            )
        return ImpactStatus.NOT_APPLICABLE, [note]

    if changeset.origin == "local" or present or fetched.verified:
        return None, []

    # ``fetched.notes`` says why the fallback did not work, when it was tried at
    # all. Reported alongside rather than instead of, because "we could not fetch
    # one either" is the more specific half of the same answer.
    return ImpactStatus.UNAVAILABLE, [
        "This change was fetched from the forge and the working directory does not "
        "hold its head commit, so there was no checkout to search for consumers.",
        *fetched.notes,
    ]


def _head_present(changeset: ChangeSet, repo: Path) -> bool:
    """Whether ``repo`` already holds the reviewed commit.

    The one probe behind two decisions -- whether the local checkout can be
    searched, and whether it is worth fetching another one -- so the two can
    never answer it differently. ``cat-file -e`` proves the object was *fetched*,
    not that it is checked out, which is why anything built on it stays limited.
    """
    if changeset.origin in {"local", "paths"}:
        return True
    head = changeset.head_sha
    return bool(head) and _git(repo, "cat-file", "-e", f"{head}^{{commit}}") is not None


def _git(repo: Path, *args: str, timeout: float = 10) -> str | None:
    """Run a git command, or ``None`` if git is unusable or the command failed."""
    try:
        done = subprocess.run(
            ("git", *args),
            cwd=repo,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return done.stdout if done.returncode == 0 else None


def _seed(changeset: ChangeSet, repo: Path, config: ImpactConfig) -> tuple[list[ImpactNode], bool]:
    """Every changed boundary worth tracing, and whether a parser read any of it.

    Parsed once here and handed down, because "a parser ran" and "a parser found
    a named symbol" are different answers. A change confined to module-level code
    yields no symbol from a file the grammar read perfectly well, and reporting
    that as an absent parser understates what the map actually knows.
    """
    nodes: list[ImpactNode] = []
    seen: set[tuple[str, str]] = set()
    parsed_any = False

    for file in changeset.files:
        if file.is_binary or file.change_type == "deleted":
            continue
        content = _content(file, repo, changeset.head_sha)
        if content is None:
            continue
        # A copy, never the changeset's own file: content read here is evidence
        # for the map and must not become diff surface. Writing it back would put
        # whole files into the compressor's budget and the anchoring path.
        file = file.model_copy(update={"new_content": content})
        tree = ast_context.parse(file.language, content)
        parsed_any = parsed_any or tree is not None
        for node in (*_symbols(file, tree), *_exports(file), *_contracts(file, tree)):
            key = (node.kind.value, node.name)
            if key in seen:
                continue
            seen.add(key)
            nodes.append(node)

    nodes.sort(key=lambda n: (_PRIORITY[n.kind], n.file, n.line))
    return nodes, parsed_any


def _content(file: ChangedFile, repo: Path, head: str) -> str | None:
    """The new text of a changed file, from the change or from the reviewed tree.

    Only the local and path sources populate ``new_content``; a merge or pull
    request arrives as hunks alone. Without the whole file there is no parse tree,
    so every forge change would seed nothing and the map would report the change
    untraceable rather than untraced. Reading it back out of the commit under
    review costs one ``git show`` per changed file and is the same text the forge
    would have sent.
    """
    if file.new_content is not None:
        return file.new_content
    if not head:
        return None
    # Sized before it is read, the way ``supply.revision`` does: a generated
    # bundle in the diff must not be pulled into memory to be discarded.
    size = (_git(repo, "cat-file", "-s", f"{head}:{file.path}") or "").strip()
    if not size.isdigit() or int(size) > MAX_FILE_BYTES:
        return None
    return _git(repo, "show", f"{head}:{file.path}")


def _symbols(file: ChangedFile, tree: Any | None) -> list[ImpactNode]:
    """Named symbols the change touched, from the parse tree.

    The mirror of ``ast_context.enclosing_symbol``: that one wants the symbol
    *containing* a hunk, so it demands full containment. Here any overlap counts,
    because a one-line edit inside a function still changes that function's
    contract for everyone who calls it.
    """
    if tree is None:
        return []

    added = file.added_lines
    if not added:
        return []

    out: list[ImpactNode] = []
    root = tree.root_node
    for node in ast_context.walk(root):
        if node is root or node.type not in ast_context.SYMBOL_TYPES:
            continue
        start, end = node.start_point[0] + 1, node.end_point[0] + 1
        if not any(start <= line <= end for line in added):
            continue
        name = ast_context.node_name(node)
        if not _traceable(name, guessed=False):
            continue
        out.append(
            ImpactNode(
                name=name,
                kind=BoundaryKind.SYMBOL,
                file=file.path,
                line=start,
                verification=Verification.PARSED,
            )
        )
    return out


def _exports(file: ChangedFile) -> list[ImpactNode]:
    """Public constants and exported declarations added by the change."""
    assert file.new_content is not None
    out: list[ImpactNode] = []
    for lineno, text in _added_lines(file):
        for languages, pattern in _EXPORTS:
            if file.language not in languages:
                continue
            match = pattern.match(text)
            if match and _traceable(match.group(1)):
                out.append(
                    ImpactNode(
                        name=match.group(1),
                        kind=BoundaryKind.EXPORT,
                        file=file.path,
                        line=lineno,
                        verification=Verification.TEXTUAL,
                    )
                )
    return out


def _contracts(file: ChangedFile, tree: Any | None) -> list[ImpactNode]:
    """Routes, events, config keys, env vars and schema fields the change touched.

    None of these is a symbol any parser will hand you -- they are names two
    unrelated files agree on by spelling them identically, which is exactly what
    makes them easy to break and invisible to a diff.
    """
    assert file.new_content is not None
    out: list[ImpactNode] = []

    for lineno, text in _added_lines(file):
        for contract in _CONTRACTS:
            if contract.languages is not None and file.language not in contract.languages:
                continue
            for match in contract.pattern.finditer(text):
                term = match.group(1)
                if _traceable(term, identifier=False):
                    out.append(
                        ImpactNode(
                            name=term,
                            kind=contract.kind,
                            file=file.path,
                            line=lineno,
                            verification=Verification.TEXTUAL,
                        )
                    )
    return [*out, *_schema_fields(file, tree)]


def _schema_fields(file: ChangedFile, tree: Any | None) -> list[ImpactNode]:
    """Fields added to a declared schema class, found with the parser rather than a regex.

    Deliberately not pattern-matched. ``name: str`` indented four spaces is the
    single most common line in a Python file, and treating every one of them as a
    contract fills the map with words. Asking the parser which class the line is
    in, and whether that class derives from a schema base, is the difference
    between a field on a serialised model and a local variable annotation.
    """
    if tree is None:
        return []

    added = file.added_lines
    out: list[ImpactNode] = []
    for node in ast_context.walk(tree.root_node):
        if node.type not in _CLASS_NODES or not _derives_from_schema(node):
            continue
        body = node.child_by_field_name("body")
        if body is None:
            continue
        for statement in body.children:
            line = statement.start_point[0] + 1
            if line not in added:
                continue
            name = _field_name(statement)
            if name is not None and _traceable(name):
                out.append(
                    ImpactNode(
                        name=name,
                        kind=BoundaryKind.SCHEMA_FIELD,
                        file=file.path,
                        line=line,
                        verification=Verification.PARSED,
                    )
                )
    return out


def _derives_from_schema(node: object) -> bool:
    """Whether a class node lists one of ``SCHEMA_BASES`` among its superclasses."""
    supers = getattr(node, "child_by_field_name", lambda _: None)("superclasses")
    if supers is None:
        return False
    raw = getattr(supers, "text", None)
    if not isinstance(raw, bytes):
        return False
    text = raw.decode("utf-8", "replace")
    return any(re.search(rf"\b{base}\b", text) for base in SCHEMA_BASES)


def _field_name(statement: object) -> str | None:
    """The name a class-body statement declares, if it declares one."""
    target = statement
    for _ in range(2):  # expression_statement wraps the assignment it contains
        children = getattr(target, "children", None)
        if getattr(target, "type", "") in _FIELD_NODES:
            break
        if not children:
            return None
        target = children[0]
    left = getattr(target, "child_by_field_name", lambda _: None)("left")
    raw = getattr(left, "text", None) if left is not None else None
    if not isinstance(raw, bytes):
        return None
    name = raw.decode("utf-8", "replace").strip()
    return name if IDENTIFIER.match(name) else None


def _added_lines(file: ChangedFile) -> list[tuple[int, str]]:
    """The text of every line the change added, with its new-file line number."""
    assert file.new_content is not None
    lines = file.new_content.splitlines()
    return [(n, lines[n - 1]) for n in sorted(file.added_lines) if 0 < n <= len(lines)]


def _traceable(name: str, *, identifier: bool = True, guessed: bool = True) -> bool:
    """Whether a name is distinctive enough that searching for it means something.

    ``guessed`` marks a name a pattern produced rather than a parser. Those are
    held to the stop list, because a regular expression cannot tell a contract
    from a common word and its mistakes cost the whole map its signal.
    """
    if len(name) < MIN_TERM_LENGTH:
        return False
    if identifier and not IDENTIFIER.match(name):
        return False
    if identifier and name in {"self", "this", "None", "true", "false"}:
        return False
    return not (guessed and name.lower() in TOO_GENERIC)


@dataclass(frozen=True)
class _Hit:
    """One line somewhere in the repository that spells a changed name."""

    path: str
    line: int
    text: str


@dataclass
class _Search:
    """Reference discovery, with one backend chosen up front and stuck to."""

    repo: Path
    config: ImpactConfig
    changed: set[str]
    method: Literal["git-grep", "walk", "none"] = "none"
    truncated: bool = False
    timed_out: bool = False
    """Which ceiling bit, so the note can name the one the reviewer can raise."""

    def find(self, terms: list[str]) -> dict[str, list[_Hit]]:
        """Every line in the repository mentioning each term, minus the change itself.

        One deadline covers the whole search rather than one per term, because a
        dozen changed symbols against a slow repository would otherwise cost a
        dozen timeouts and `impact.timeout_seconds` would bound nothing a
        reviewer actually waits on. What the budget does not reach is reported as
        truncated, the same as any other ceiling.
        """
        deadline = time.monotonic() + self.config.timeout_seconds
        found: dict[str, list[_Hit] | None]
        if _git(self.repo, "rev-parse", "--git-dir", timeout=self.config.timeout_seconds) is None:
            self.method = "walk"
            found = dict(self._walk(terms, deadline))
        else:
            self.method = "git-grep"
            found = {term: [] for term in terms}
            for term in terms:
                if self._expired(deadline):
                    break
                found[term] = self._git_grep(term, deadline)
            if not self.timed_out and any(hits is None for hits in found.values()):
                # git answered `rev-parse` and then failed on a search: rather than
                # report half a map, redo the lot the slow way so every node was
                # searched the same way and the method on the map is the truth.
                # A search that merely ran out of clock is not that: there is no
                # budget left to redo it in, and the terms already searched are
                # worth more than an empty map.
                self.method = "walk"
                found = dict(self._walk(terms, deadline))

        return {
            term: [hit for hit in (hits or []) if self._eligible(hit)]
            for term, hits in found.items()
        }

    def _eligible(self, hit: _Hit) -> bool:
        """A hit is a candidate consumer unless it is the change itself, or prose."""
        return hit.path not in self.changed and detect_language(hit.path) not in PROSE_LANGUAGES

    def _git_grep(self, term: str, deadline: float) -> list[_Hit] | None:
        """Tracked *and* untracked-but-not-ignored files, so a repo with nothing
        committed yet and a review run with ``--include-untracked`` both still
        search the code that is actually there."""
        args = ["grep", "-n", "-I", "--fixed-strings", "--untracked", "--exclude-standard"]
        # `-w` anchors at word boundaries, which is what stops `run` matching
        # `rerun`. A route or an event name starts with punctuation, where the
        # flag's semantics stop being what we want, so it is only for identifiers.
        if IDENTIFIER.match(term):
            args.append("-w")
        try:
            done = subprocess.run(
                ["git", *args, "--", term],
                cwd=self.repo,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self._left(deadline),
                check=False,
            )
        except subprocess.TimeoutExpired:
            self.truncated = True
            self.timed_out = True
            return None
        except OSError:
            return None
        if done.returncode > 1:  # 1 is "no matches", which is an answer
            return None
        return _parse_grep(done.stdout)

    def _walk(self, terms: list[str], deadline: float) -> dict[str, list[_Hit]]:
        """One pass over the directory, matching every term as it goes.

        Deliberately one pass rather than one per term: the fallback is already
        the slow path, and re-reading every file once per changed symbol would
        make it the unusable one.

        Bounded twice over, because a file count is not a bound on the work: one
        half-megabyte file matched against a dozen terms is a lot of regex, and a
        tree of empty directories is a walk that scans nothing and still takes
        time. The wall clock is the ceiling that holds in both cases -- the same
        one the caller opened, not a fresh one -- and this is the path taken when
        git has *already* failed: not the moment to run for as long as it takes.
        """
        matchers = {term: _matcher(term) for term in terms}
        found: dict[str, list[_Hit]] = {term: [] for term in terms}
        scanned = 0

        for dirpath, dirnames, filenames in os.walk(self.repo, followlinks=False):
            if self._expired(deadline):
                return found
            here = Path(dirpath)
            dirnames[:] = sorted(
                name
                for name in dirnames
                if name not in SKIP_DIRS and not (here / name).is_symlink()
            )
            for name in sorted(filenames):
                if scanned >= self.config.max_files_scanned:
                    self.truncated = True
                    return found
                if self._expired(deadline):
                    return found
                path = here / name
                relative = path.relative_to(self.repo).as_posix()
                if path.is_symlink():
                    continue
                try:
                    if path.stat().st_size > MAX_FILE_BYTES:
                        continue
                    text = path.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    continue
                scanned += 1
                for lineno, line in enumerate(text.splitlines(), start=1):
                    if self._expired(deadline):
                        return found
                    for term, matcher in matchers.items():
                        if matcher.search(line):
                            found[term].append(_Hit(relative, lineno, line))
        return found

    def _left(self, deadline: float) -> float:
        """Whatever is left of the one budget, for a call that takes a timeout."""
        return max(0.0, deadline - time.monotonic())

    def _expired(self, deadline: float) -> bool:
        """Whether the wall clock has run out, recording it as it answers.

        Checked between files and between lines rather than only between files,
        so a single large file cannot outlast the deadline on its own.
        """
        if time.monotonic() < deadline:
            return False
        self.truncated = True
        self.timed_out = True
        return True


def _matcher(term: str) -> re.Pattern[str]:
    escaped = re.escape(term)
    return re.compile(rf"\b{escaped}\b" if IDENTIFIER.match(term) else escaped)


def _parse_grep(output: str) -> list[_Hit]:
    """``path:line:text`` into hits, skipping anything that is not that shape."""
    hits: list[_Hit] = []
    for row in output.splitlines():
        head, _, text = row.partition(":")
        number, _, text = text.partition(":")
        if not head or not number.isdigit():
            continue
        hits.append(_Hit(head, int(number), text))
    return hits


_RELATION_RANK: dict[ConsumerRelation, int] = {
    ConsumerRelation.CALL: 0,
    ConsumerRelation.IMPORT: 1,
    ConsumerRelation.TEST: 2,
    ConsumerRelation.CONFIG: 3,
}
"""Which consumers are worth the per-node cap first.

A call site is what tells a reviewer whether a signature change breaks anything.
A word matching inside a YAML issue template is a coincidence, and left in file
order it takes a slot from the caller that matters -- ``.github`` sorts before
``src``. Ranking rather than excluding: a configuration file really can be a
consumer of a configuration key, and it should be shown once the calls are in.
"""


def _resolve(node: ImpactNode, hits: list[_Hit], search: _Search) -> None:
    """Turn raw matches into consumers, and say what that establishes."""
    config = search.config
    hits = sorted(hits, key=lambda hit: _RELATION_RANK[_relation(hit)])
    verified: list[_Hit] = []
    literals = 0
    parsed_any = False
    checked = 0
    budget = config.max_consumers_per_node * 3

    for hit in hits:
        if checked < budget:
            checked += 1
            outcome = _confirm(search.repo, hit, node.name)
            if outcome is False:
                literals += 1
                continue
            parsed_any = parsed_any or outcome is True
        verified.append(hit)
        if len(verified) >= config.max_consumers_per_node and checked >= budget:
            break

    node.truncated = len(verified) > config.max_consumers_per_node
    kept = verified[: config.max_consumers_per_node]
    node.consumers = [_consumer(search.repo, hit, config) for hit in kept]

    notes: list[str] = []
    if node.truncated:
        notes.append(
            f"{len(hits)} candidate matches found; the first "
            f"{config.max_consumers_per_node} are shown."
        )
    if literals:
        notes.append(
            f"{literals} match(es) in comments or string literals were not counted as uses."
        )

    if node.consumers:
        node.status = ImpactStatus.CONSUMERS_FOUND
        node.verification = Verification.PARSED if parsed_any else Verification.TEXTUAL
    elif _can_claim_containment(node, search, literals):
        node.status = ImpactStatus.CONTAINED
        node.verification = Verification.PARSED
        notes.append(
            "No use found anywhere in the repository. Consumers outside it, and names "
            "assembled at runtime, would not appear here."
        )
    else:
        node.status = ImpactStatus.NO_REFERENCES_FOUND
        node.verification = Verification.TEXTUAL
        notes.append(
            "Nothing matched by name, which is not the same as unused: an alias, a "
            "re-export, generated code or a dynamic reference would not match."
        )
    node.note = " ".join(notes)


def _can_claim_containment(node: ImpactNode, search: _Search, literals: int) -> bool:
    """Whether "contained" is a claim the evidence actually supports.

    Four things have to hold at once, and the issue this implements is explicit
    that text search alone is never enough. A parser had to identify the symbol,
    the search had to cover the whole repository without truncating, and nothing
    -- not even a mention in a string, which is what a dynamic reference looks
    like -- can have matched.
    """
    return (
        node.kind is BoundaryKind.SYMBOL
        and node.verification is Verification.PARSED
        and search.method == "git-grep"
        and not search.truncated
        and literals == 0
    )


def _confirm(repo: Path, hit: _Hit, term: str) -> bool | None:
    """``True`` a parser calls it code, ``False`` a literal, ``None`` no parser.

    The distinction the whole map rests on: finding the characters and finding a
    *use* of them are different claims, and only the second one can support
    containment.
    """
    language = detect_language(hit.path)
    column = hit.text.find(term)
    if language is None or column < 0:
        return None
    try:
        source = (repo / hit.path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    tree = ast_context.parse(language, source)
    if tree is None:
        return None
    found = ast_context.node_at(tree, hit.line - 1, column)
    if found is None:
        return None
    return found.type not in _LITERAL_NODES


def _consumer(repo: Path, hit: _Hit, config: ImpactConfig) -> Consumer:
    context = max(0, (config.max_snippet_lines - 1) // 2)
    read = window(
        repo, hit.path, hit.line, hit.line, context=context, limit=config.max_snippet_lines
    )
    return Consumer(
        path=hit.path,
        line=hit.line,
        relation=_relation(hit),
        snippet=read[0] if read else hit.text.strip(),
    )


def _relation(hit: _Hit) -> ConsumerRelation:
    """How this consumer touches the name, which changes what a break means to it."""
    if _TEST_PATH.search(hit.path):
        return ConsumerRelation.TEST
    if _IMPORT_LINE.match(hit.text):
        return ConsumerRelation.IMPORT
    language = detect_language(hit.path)
    # A file we cannot even name the language of is not a call site. It may still
    # be a genuine consumer -- a Makefile reads environment variables, a Dockerfile
    # names them -- so it is ranked with configuration rather than discarded.
    if language is None or language in _CONFIG_LANGUAGES:
        return ConsumerRelation.CONFIG
    return ConsumerRelation.CALL


def _map_status(nodes: list[ImpactNode], *, limited: bool) -> ImpactStatus:
    if limited:
        return ImpactStatus.LIMITED
    if any(node.consumers for node in nodes):
        return ImpactStatus.CONSUMERS_FOUND
    if nodes and all(node.status is ImpactStatus.CONTAINED for node in nodes):
        return ImpactStatus.CONTAINED
    return ImpactStatus.NO_REFERENCES_FOUND


CHARS_PER_TOKEN = 4
"""What a token costs, near enough to hold a budget without a tokeniser.

The context layer has no model client and should not grow one for this. Four is
the conservative direction: it over-estimates the section on code, so the
reservation the reviewer makes is never smaller than what gets rendered."""


def _fit_budget(nodes: list[ImpactNode], config: ImpactConfig, notes: list[str]) -> bool:
    """Trim the map until its prompt section fits, snippets first, nodes last."""
    ceiling = config.token_budget * CHARS_PER_TOKEN
    if _size(nodes) <= ceiling:
        return False

    for node in reversed(nodes):
        for consumer in node.consumers:
            consumer.snippet = ""
        if _size(nodes) <= ceiling:
            notes.append("Some consumer snippets were dropped to fit the context budget.")
            return True

    while len(nodes) > 1 and _size(nodes) > ceiling:
        nodes.pop()
    notes.append(
        f"The blast-radius map was trimmed to {len(nodes)} boundary(s) to fit "
        f"`impact.token_budget`."
    )
    return True


def _size(nodes: list[ImpactNode]) -> int:
    return sum(
        len(node.name)
        + len(node.file)
        + len(node.note)
        + 24
        + sum(len(consumer.path) + len(consumer.snippet) + 16 for consumer in node.consumers)
        for node in nodes
    )


def for_prompt(impact: ImpactMap | None, paths: set[str]) -> list[dict[str, Any]]:
    """The map as the review prompt sees it, narrowed to the files in this pass.

    A chunked review shows the model one part of the change at a time, and a
    consumer of a symbol it cannot see is noise it cannot act on.
    """
    if impact is None or not impact.nodes:
        return []
    return [
        {
            "name": node.name,
            "kind": node.kind.value.replace("_", " "),
            "file": node.file,
            "line": node.line,
            "status": node.status.value.replace("_", " "),
            "note": node.note,
            "consumers": [
                {
                    "path": consumer.path,
                    "line": consumer.line,
                    "relation": consumer.relation.value,
                    "snippet": consumer.snippet,
                }
                for consumer in node.consumers
            ],
        }
        for node in impact.nodes
        if node.file in paths
    ]
