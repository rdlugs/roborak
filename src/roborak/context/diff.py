"""Unified-diff parsing.

This is the component the rest of roborak trusts absolutely: if ``line_map`` is
wrong, inline comments land on the wrong lines of somebody's merge request. It is
therefore deliberately hand-written rather than delegated, and covered by golden
fixtures in ``tests/test_diff.py``.

Diff position semantics
-----------------------
GitHub and GitLab both anchor inline comments by *position*: the 1-based index of
a line within the file's diff body, counting every ``@@`` header and every
context/added/removed line, and continuing across hunks. We compute that here,
once, while the diff is in front of us.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath

from roborak.core.models import ChangedFile, ChangeType, Hunk

_HUNK_RE = re.compile(
    r"^@@ -(?P<old_start>\d+)(?:,(?P<old_lines>\d+))? "
    r"\+(?P<new_start>\d+)(?:,(?P<new_lines>\d+))? @@(?P<header>.*)$"
)

# Extensions we can name a language for. Anything absent stays ``None`` and is
# still reviewed -- this only informs prompt hints and static-tool selection.
_LANG_BY_SUFFIX: dict[str, str] = {
    ".py": "python", ".pyi": "python",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".ts": "typescript", ".tsx": "typescript",
    ".php": "php",
    ".go": "go",
    ".rs": "rust",
    ".rb": "ruby",
    ".java": "java",
    ".kt": "kotlin", ".kts": "kotlin",
    ".cs": "csharp",
    ".c": "c", ".h": "c",
    ".cpp": "cpp", ".cc": "cpp", ".cxx": "cpp", ".hpp": "cpp",
    ".swift": "swift",
    ".scala": "scala",
    ".sh": "shell", ".bash": "shell", ".zsh": "shell",
    ".sql": "sql",
    ".yaml": "yaml", ".yml": "yaml",
    ".json": "json",
    ".toml": "toml",
    ".md": "markdown",
    ".html": "html",
    ".css": "css", ".scss": "scss",
    ".vue": "vue",
    ".tf": "terraform",
}


def detect_language(path: str) -> str | None:
    return _LANG_BY_SUFFIX.get(PurePosixPath(path).suffix.lower())


def parse_diff(diff_text: str) -> list[ChangedFile]:
    """Parse a multi-file unified diff into ``ChangedFile`` objects."""
    return [_parse_file_block(block) for block in _split_file_blocks(diff_text) if block]


def _split_file_blocks(diff_text: str) -> list[list[str]]:
    """Split on ``diff --git`` boundaries, tolerating diffs that lack that header."""
    blocks: list[list[str]] = []
    current: list[str] = []
    for line in diff_text.splitlines():
        if line.startswith("diff --git ") or (not current and line.startswith("--- ")):
            if current:
                blocks.append(current)
            current = [line]
        elif current:
            current.append(line)
    if current:
        blocks.append(current)
    return blocks


def _parse_file_block(lines: list[str]) -> ChangedFile:
    old_path, new_path = _paths_from_header(lines)
    change_type: ChangeType = "modified"
    is_binary = False

    for line in lines:
        if line.startswith("new file mode"):
            change_type = "added"
        elif line.startswith("deleted file mode"):
            change_type = "deleted"
        elif line.startswith("rename to "):
            change_type = "renamed"
            new_path = line[len("rename to "):].strip()
        elif line.startswith("rename from "):
            old_path = line[len("rename from "):].strip()
        elif line.startswith("Binary files ") or line.startswith("GIT binary patch"):
            is_binary = True
        elif line.startswith("@@"):
            break

    path = new_path or old_path or ""
    previous = old_path if (change_type == "renamed" and old_path != new_path) else None

    return ChangedFile(
        path=path,
        previous_path=previous,
        change_type=change_type,
        language=detect_language(path),
        hunks=[] if is_binary else _parse_hunks(lines),
        is_binary=is_binary,
    )


def _paths_from_header(lines: list[str]) -> tuple[str | None, str | None]:
    """Recover both paths, preferring ``---``/``+++`` over the ``diff --git`` line.

    The ``diff --git`` line is ambiguous when a path contains a space; the
    ``---``/``+++`` lines are not, so they win when present.
    """
    old_path: str | None = None
    new_path: str | None = None

    for line in lines:
        if line.startswith("--- "):
            old_path = _strip_prefix(line[4:].strip())
        elif line.startswith("+++ "):
            new_path = _strip_prefix(line[4:].strip())
        elif line.startswith("@@"):
            break

    if old_path is None and new_path is None and lines and lines[0].startswith("diff --git "):
        parts = lines[0][len("diff --git "):].split(" b/", 1)
        if len(parts) == 2:
            old_path = _strip_prefix(parts[0])
            new_path = _strip_prefix("b/" + parts[1])

    return old_path, new_path


def _strip_prefix(path: str) -> str | None:
    """Drop git's ``a/``/``b/`` prefix; ``/dev/null`` becomes ``None``."""
    path = path.strip()
    if path == "/dev/null":
        return None
    # A trailing tab-separated timestamp appears in some `diff -u` output.
    path = path.split("\t", 1)[0]
    if path.startswith(("a/", "b/")):
        return path[2:]
    return path


def _parse_hunks(lines: list[str]) -> list[Hunk]:
    """Build hunks, tracking new-file line numbers and cross-hunk diff positions."""
    hunks: list[Hunk] = []
    position = 0  # counts every line of the diff body, headers included
    index = 0

    while index < len(lines):
        match = _HUNK_RE.match(lines[index])
        if not match:
            index += 1
            continue

        position += 1  # the @@ header itself occupies a position
        new_lineno = int(match["new_start"])
        hunk = Hunk(
            old_start=int(match["old_start"]),
            old_lines=int(match["old_lines"] or 1),
            new_start=new_lineno,
            new_lines=int(match["new_lines"] or 1),
            header=(match["header"] or "").strip(),
            content="",
        )
        body: list[str] = []
        index += 1

        while index < len(lines):
            line = lines[index]
            if _HUNK_RE.match(line) or line.startswith("diff --git "):
                break
            # "\ No newline at end of file" is metadata, not a diff line: it
            # occupies no position and advances no line counter.
            if line.startswith("\\"):
                body.append(line)
                index += 1
                continue

            position += 1
            body.append(line)

            if line.startswith("+"):
                hunk.line_map[new_lineno] = position
                hunk.added_lines.add(new_lineno)
                new_lineno += 1
            elif line.startswith("-"):
                pass  # removed lines advance only the old-file counter
            else:
                # Context line (leading space, or a bare empty line some tools emit).
                hunk.line_map[new_lineno] = position
                new_lineno += 1

            index += 1

        hunk.content = "\n".join(body)
        hunks.append(hunk)

    return hunks


def render_hunk_with_line_numbers(hunk: Hunk, max_lines: int | None = None) -> str:
    """Render a hunk annotated with new-file line numbers, for the LLM prompt.

    Giving the model explicit numbers is what lets it return anchors we can trust
    instead of counting lines itself.

    ``max_lines`` trims surplus context from an oversized hunk. Trimming happens
    here, after numbering, so the numbers stay correct no matter how much context
    is dropped -- doing it earlier on the raw diff body would silently renumber
    every line below the cut.
    """
    numbered: list[tuple[int | None, str]] = []
    lineno = hunk.new_start
    for line in hunk.content.splitlines():
        if line.startswith("\\"):
            continue  # "\ No newline at end of file" is metadata
        if line.startswith("-"):
            numbered.append((None, line))
        else:
            numbered.append((lineno, line))
            lineno += 1

    if max_lines is not None and len(numbered) > max_lines:
        numbered = _trim_numbered(numbered, max_lines)

    out: list[str] = [f"@@ {hunk.header}".rstrip()]
    for number, line in numbered:
        if number is None and line.startswith("."):
            out.append(line)  # an omission marker
        elif number is None:
            out.append(f"{'':>6} {line}")
        else:
            out.append(f"{number:>6} {line}")
    return "\n".join(out)


def _trim_numbered(
    numbered: list[tuple[int | None, str]], max_lines: int
) -> list[tuple[int | None, str]]:
    """Keep changed lines plus a window of context, marking what was dropped."""
    changed = [i for i, (_, line) in enumerate(numbered) if line.startswith(("+", "-"))]
    if not changed:
        return numbered[:max_lines]

    # Shrink the context window until the result fits, but never below one line.
    for context in (6, 4, 3, 2, 1):
        keep: set[int] = set()
        for index in changed:
            keep.update(range(max(0, index - context), min(len(numbered), index + context + 1)))
        if len(keep) <= max_lines:
            break

    out: list[tuple[int | None, str]] = []
    previous = -1
    for index in sorted(keep):
        if previous >= 0 and index > previous + 1:
            gap = index - previous - 1
            out.append((None, f"... {gap} unchanged lines omitted ..."))
        out.append(numbered[index])
        previous = index
    return out
