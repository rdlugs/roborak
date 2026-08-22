"""Golden fixtures for diff parsing.

Line mapping is the one thing roborak cannot get wrong -- a bad ``line_map``
posts review comments on unrelated lines of somebody's merge request -- so the
positions below are asserted against hand-counted values.
"""

from __future__ import annotations

import textwrap

from roborak.context.diff import (
    detect_language,
    parse_diff,
    render_hunk_with_line_numbers,
)

SIMPLE = textwrap.dedent(
    """\
    diff --git a/src/app.py b/src/app.py
    index 1234567..89abcde 100644
    --- a/src/app.py
    +++ b/src/app.py
    @@ -10,6 +10,7 @@ def handler(request):
         user = request.user
         if not user:
             return None
    +    log.info("resolved %s", user.id)
         session = load(user)
         return session
    """
)


def test_simple_modification_paths_and_type():
    files = parse_diff(SIMPLE)
    assert len(files) == 1
    f = files[0]
    assert f.path == "src/app.py"
    assert f.change_type == "modified"
    assert f.language == "python"
    assert f.previous_path is None
    assert not f.is_binary


def test_simple_modification_line_numbers():
    f = parse_diff(SIMPLE)[0]
    hunk = f.hunks[0]
    assert (hunk.old_start, hunk.old_lines) == (10, 6)
    assert (hunk.new_start, hunk.new_lines) == (10, 7)
    assert hunk.header == "def handler(request):"
    assert f.added_lines == {13}


def test_simple_modification_diff_positions():
    """Position 1 is the @@ header; body lines follow in order."""
    f = parse_diff(SIMPLE)[0]
    hunk = f.hunks[0]
    assert hunk.line_map == {10: 2, 11: 3, 12: 4, 13: 5, 14: 6, 15: 7}
    assert f.diff_position(13) == 5
    assert f.diff_position(999) is None


MULTI_HUNK = textwrap.dedent(
    """\
    diff --git a/app.py b/app.py
    --- a/app.py
    +++ b/app.py
    @@ -1,3 +1,4 @@
     import os
    +import sys
     import json
     import re
    @@ -20,7 +21,7 @@ class Thing:
         def run(self):
             data = load()
    -        return data
    +        return validate(data)

         def stop(self):
    """
)


def test_positions_continue_across_hunks():
    f = parse_diff(MULTI_HUNK)[0]
    first, second = f.hunks

    assert first.line_map == {1: 2, 2: 3, 3: 4, 4: 5}
    assert first.added_lines == {2}

    assert second.new_start == 21
    assert second.line_map[21] == 7
    assert second.line_map[22] == 8
    assert second.line_map[23] == 10
    assert second.added_lines == {23}
    assert f.added_lines == {2, 23}


def test_added_file():
    diff = textwrap.dedent(
        """\
        diff --git a/new.py b/new.py
        new file mode 100644
        index 0000000..e69de29
        --- /dev/null
        +++ b/new.py
        @@ -0,0 +1,3 @@
        +def a():
        +    return 1
        +
        """
    )
    f = parse_diff(diff)[0]
    assert f.change_type == "added"
    assert f.path == "new.py"
    assert f.added_lines == {1, 2, 3}
    assert f.hunks[0].line_map == {1: 2, 2: 3, 3: 4}


def test_deleted_file_keeps_old_path():
    diff = textwrap.dedent(
        """\
        diff --git a/gone.py b/gone.py
        deleted file mode 100644
        --- a/gone.py
        +++ /dev/null
        @@ -1,2 +0,0 @@
        -def a():
        -    return 1
        """
    )
    f = parse_diff(diff)[0]
    assert f.change_type == "deleted"
    assert f.path == "gone.py"
    assert f.added_lines == set()


def test_rename_records_previous_path():
    diff = textwrap.dedent(
        """\
        diff --git a/old/name.py b/new/name.py
        similarity index 92%
        rename from old/name.py
        rename to new/name.py
        --- a/old/name.py
        +++ b/new/name.py
        @@ -5,3 +5,3 @@
         a
        -b
        +c
        """
    )
    f = parse_diff(diff)[0]
    assert f.change_type == "renamed"
    assert f.path == "new/name.py"
    assert f.previous_path == "old/name.py"
    assert f.added_lines == {6}


def test_binary_file_has_no_hunks():
    diff = textwrap.dedent(
        """\
        diff --git a/logo.png b/logo.png
        index 1111111..2222222 100644
        Binary files a/logo.png and b/logo.png differ
        """
    )
    f = parse_diff(diff)[0]
    assert f.is_binary
    assert f.hunks == []


def test_no_newline_marker_does_not_consume_a_position():
    diff = textwrap.dedent(
        """\
        diff --git a/f.txt b/f.txt
        --- a/f.txt
        +++ b/f.txt
        @@ -1,2 +1,2 @@
         keep
        -old
        \\ No newline at end of file
        +new
        \\ No newline at end of file
        """
    )
    f = parse_diff(diff)[0]
    hunk = f.hunks[0]
    assert hunk.line_map == {1: 2, 2: 4}
    assert hunk.added_lines == {2}


def test_multiple_files_in_one_diff():
    diff = SIMPLE + textwrap.dedent(
        """\
        diff --git a/other.ts b/other.ts
        --- a/other.ts
        +++ b/other.ts
        @@ -1,1 +1,2 @@
         const a = 1;
        +const b = 2;
        """
    )
    files = parse_diff(diff)
    assert [f.path for f in files] == ["src/app.py", "other.ts"]
    assert files[1].language == "typescript"
    assert files[1].hunks[0].line_map == {1: 2, 2: 3}


def test_hunk_lookup_helpers():
    f = parse_diff(SIMPLE)[0]
    assert f.hunk_for_line(13) is f.hunks[0]
    assert f.hunk_for_line(500) is None


def test_render_hunk_with_line_numbers():
    f = parse_diff(SIMPLE)[0]
    rendered = render_hunk_with_line_numbers(f.hunks[0])
    lines = rendered.splitlines()
    assert lines[0] == "@@ def handler(request):"
    assert lines[1].strip().startswith("10 ")
    assert "13 +    log.info" in rendered


def test_removed_lines_render_without_a_number():
    f = parse_diff(MULTI_HUNK)[0]
    rendered = render_hunk_with_line_numbers(f.hunks[1])
    removed = [ln for ln in rendered.splitlines() if ln.lstrip().startswith("-")]
    assert removed == ["       -        return data"]


def test_detect_language():
    assert detect_language("a/b/c.php") == "php"
    assert detect_language("Makefile") is None


def _numbers_in(rendered: str) -> list[int]:
    """Line numbers roborak told the model to use."""
    out = []
    for line in rendered.splitlines():
        head = line[:6].strip()
        if head.isdigit():
            out.append(int(head))
    return out


def test_trimming_preserves_line_numbers():
    """The whole point of trimming at render time: numbers must survive it."""
    body = ["diff --git a/big.py b/big.py", "--- a/big.py", "+++ b/big.py", "@@ -1,60 +1,61 @@"]
    body += [f" line {i}" for i in range(1, 30)]
    body += ["+inserted"]
    body += [f" line {i}" for i in range(30, 60)]
    f = parse_diff("\n".join(body))[0]
    hunk = f.hunks[0]

    assert hunk.added_lines == {30}
    assert 30 in _numbers_in(render_hunk_with_line_numbers(hunk))

    trimmed = render_hunk_with_line_numbers(hunk, max_lines=20)
    numbers = _numbers_in(trimmed)
    assert 30 in numbers, "the added line must survive trimming"
    assert "    30 +inserted" in trimmed
    assert "    31  line 30" in trimmed
    assert numbers == sorted(numbers)
    assert len(numbers) < 61


def test_trimming_is_a_noop_when_under_the_limit():
    f = parse_diff(SIMPLE)[0]
    assert render_hunk_with_line_numbers(
        f.hunks[0], max_lines=100
    ) == render_hunk_with_line_numbers(f.hunks[0])


def test_trimming_keeps_context_around_every_change():
    body = ["diff --git a/x.py b/x.py", "--- a/x.py", "+++ b/x.py", "@@ -1,40 +1,42 @@"]
    body += [f" a{i}" for i in range(20)]
    body += ["+first"]
    body += [f" b{i}" for i in range(20)]
    body += ["+second"]
    f = parse_diff("\n".join(body))[0]
    rendered = render_hunk_with_line_numbers(f.hunks[0], max_lines=25)
    assert "+first" in rendered and "+second" in rendered
    assert "unchanged lines omitted" in rendered
