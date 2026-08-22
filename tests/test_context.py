"""AST context and chunking."""

from __future__ import annotations

import textwrap

import pytest

from roborak.context import ast_context
from roborak.context.chunker import MAX_CHUNKS, chunk, needs_chunking
from roborak.context.diff import parse_diff
from roborak.core.models import ChangedFile, ChangeSet, Hunk

requires_tree_sitter = pytest.mark.skipif(
    not ast_context.available(), reason="tree-sitter not installed"
)

PYTHON_SRC = textwrap.dedent(
    """\
    import os


    class Service:
        def __init__(self):
            self.cache = {}

        def run(self, target):
            data = load(target)
            checked = validate(data)
            return checked


    def helper(x):
        return x + 1
    """
)


def hunk_at(start: int, lines: int = 1) -> Hunk:
    return Hunk(old_start=start, old_lines=lines, new_start=start, new_lines=lines, content="")


@requires_tree_sitter
def test_finds_the_innermost_symbol():
    """The function, not the class that contains it."""
    file = ChangedFile(path="svc.py", language="python", new_content=PYTHON_SRC)
    span = ast_context.enclosing_symbol(file, hunk_at(9))
    assert span is not None
    assert span.name == "run"
    assert span.kind == "function_definition"
    assert span.start_line == 8


@requires_tree_sitter
def test_finds_a_module_level_function():
    file = ChangedFile(path="svc.py", language="python", new_content=PYTHON_SRC)
    span = ast_context.enclosing_symbol(file, hunk_at(15))
    assert span is not None and span.name == "helper"


@requires_tree_sitter
def test_no_symbol_at_module_level():
    file = ChangedFile(path="svc.py", language="python", new_content=PYTHON_SRC)
    assert ast_context.enclosing_symbol(file, hunk_at(1)) is None


@requires_tree_sitter
def test_a_hunk_spanning_two_functions_falls_back_to_the_class():
    file = ChangedFile(path="svc.py", language="python", new_content=PYTHON_SRC)
    span = ast_context.enclosing_symbol(
        file, Hunk(old_start=5, old_lines=7, new_start=5, new_lines=7, content="")
    )
    assert span is not None and span.name == "Service"


@requires_tree_sitter
def test_symbol_context_is_a_readable_note():
    file = ChangedFile(path="svc.py", language="python", new_content=PYTHON_SRC)
    assert ast_context.symbol_context(file, hunk_at(9)) == "within function `run` (lines 8-11)"


@requires_tree_sitter
def test_an_oversized_symbol_is_not_reported():
    """Naming a 500-line function does not help; it just costs tokens."""
    big = "def enormous():\n" + "\n".join(f"    x{i} = {i}" for i in range(300))
    file = ChangedFile(path="big.py", language="python", new_content=big)
    assert ast_context.enclosing_symbol(file, hunk_at(50)) is None


@requires_tree_sitter
@pytest.mark.parametrize(
    ("language", "source", "line", "expected"),
    [
        ("javascript", "function outer() {\n  const a = 1;\n  return a;\n}\n", 2, "outer"),
        ("go", "package m\n\nfunc Run(x int) int {\n\treturn x\n}\n", 4, "Run"),
        ("rust", "fn compute(x: i32) -> i32 {\n    x + 1\n}\n", 2, "compute"),
    ],
)
def test_other_languages(language, source, line, expected):
    file = ChangedFile(path=f"a.{language}", language=language, new_content=source)
    span = ast_context.enclosing_symbol(file, hunk_at(line))
    assert span is not None and span.name == expected


def test_missing_content_or_language_is_survivable():
    assert ast_context.enclosing_symbol(ChangedFile(path="a.py"), hunk_at(1)) is None
    assert (
        ast_context.enclosing_symbol(
            ChangedFile(path="a.xyz", language=None, new_content="x"), hunk_at(1)
        )
        is None
    )


@requires_tree_sitter
def test_a_syntactically_broken_file_does_not_raise():
    """Mid-review, half-written code is normal."""
    file = ChangedFile(path="a.py", language="python", new_content="def broken(:\n  ???\n")
    assert ast_context.symbol_context(file, hunk_at(1)) == ""


@requires_tree_sitter
def test_an_unknown_language_is_survivable():
    file = ChangedFile(path="a.zzz", language="klingon", new_content="whatever")
    assert ast_context.enclosing_symbol(file, hunk_at(1)) is None


def make_file(path: str, lines: int, language: str = "python") -> ChangedFile:
    body = "\n".join(f"+line {i}" for i in range(lines))
    hunk = Hunk(old_start=1, old_lines=1, new_start=1, new_lines=lines, content=body)
    return ChangedFile(path=path, language=language, hunks=[hunk])


def render(file: ChangedFile) -> str:
    return "\n".join(h.content for h in file.hunks)


def count(text: str) -> int:
    return len(text) // 4


def test_a_small_change_needs_no_chunking():
    changeset = ChangeSet(files=[make_file("a.py", 5)])
    assert not needs_chunking(changeset, 10_000, count, render)


def test_a_large_change_needs_chunking():
    changeset = ChangeSet(files=[make_file(f"f{i}.py", 200) for i in range(10)])
    assert needs_chunking(changeset, 100, count, render)


def test_chunks_each_fit_the_budget():
    files = [make_file(f"pkg/f{i}.py", 40) for i in range(10)]
    changeset = ChangeSet(files=files, title="Big change")
    budget = count(render(files[0])) * 3

    chunks = chunk(changeset, budget, count, render)
    assert len(chunks) > 1
    for piece in chunks:
        joined = count("\n".join(render(f) for f in piece.files))
        assert joined <= budget or len(piece.files) == 1, (
            f"chunk of {len(piece.files)} files costs {joined} > {budget}"
        )


def test_every_file_lands_in_exactly_one_chunk():
    files = [make_file(f"pkg/f{i}.py", 40) for i in range(10)]
    chunks = chunk(ChangeSet(files=files), count(render(files[0])) * 3, count, render)
    landed = [f.path for piece in chunks for f in piece.files]
    assert sorted(landed) == sorted(f.path for f in files)
    assert len(landed) == len(set(landed)), "no file may be reviewed twice"


def test_chunks_inherit_the_parents_metadata():
    """Each pass must be able to produce properly anchored, attributable findings."""
    from roborak.core.models import ForgeRef

    forge_ref = ForgeRef(provider="gitlab", host="h", project="p", number=1)
    changeset = ChangeSet(
        files=[make_file(f"f{i}.py", 40) for i in range(6)],
        title="Big change",
        description="Why it matters.",
        base_sha="base",
        head_sha="head",
        origin="gitlab",
        forge_ref=forge_ref,
    )
    for piece in chunk(changeset, 30, count, render):
        assert piece.title == "Big change"
        assert piece.description == "Why it matters."
        assert piece.base_sha == "base"
        assert piece.origin == "gitlab"
        assert piece.forge_ref is forge_ref


def test_files_are_grouped_by_directory():
    files = [
        make_file("app/auth/a.py", 20),
        make_file("docs/x.py", 20),
        make_file("app/auth/b.py", 20),
    ]
    chunks = chunk(ChangeSet(files=files), count(render(files[0])) * 2, count, render)
    first = [f.path for f in chunks[0].files]
    assert first == ["app/auth/a.py", "app/auth/b.py"], "related files belong together"


def test_a_single_oversized_file_is_split_into_reviewable_windows():
    changeset = ChangeSet(files=[make_file("huge.py", 5000)])
    chunks = chunk(changeset, 10, count, render)
    assert len(chunks) == MAX_CHUNKS
    assert all(piece.files[0].path == "huge.py" for piece in chunks)
    assert chunks[0].omitted_files == ["huge.py"]


def test_chunk_count_is_capped_and_omissions_recorded():
    files = [make_file(f"f{i:03d}.py", 100) for i in range(40)]
    chunks = chunk(ChangeSet(files=files), 30, count, render)
    assert len(chunks) == MAX_CHUNKS
    assert chunks[0].omitted_files, "dropped files must be reported, not silently lost"


def test_an_empty_changeset_yields_no_chunks():
    assert chunk(ChangeSet(), 100, count, render) == []


def test_multi_pass_review_merges_findings(tmp_path):
    """A large change gets a complete review, not a partial one."""
    from roborak.analysis.reviewer import Reviewer
    from roborak.core.config import Config
    from tests.test_pipeline import StubLLM

    diff = "".join(
        f"diff --git a/pkg{i}/mod.py b/pkg{i}/mod.py\n"
        f"--- a/pkg{i}/mod.py\n+++ b/pkg{i}/mod.py\n"
        f"@@ -1,1 +1,2 @@\n x = 1\n+y{i} = 2\n"
        for i in range(6)
    )
    changeset = ChangeSet(files=parse_diff(diff))

    import re

    from roborak.llm.client import LLMResponse

    class Counting(StubLLM):
        calls = 0

        def complete(self, system, user):
            Counting.calls += 1
            path = re.search(r"### (\S+)", user).group(1)
            return LLMResponse(
                text=(
                    "findings:\n"
                    f"  - file: {path}\n"
                    "    start_line: 2\n"
                    "    severity: major\n"
                    "    category: bug\n"
                    "    confidence: 0.9\n"
                    f"    body: Something wrong in {path}.\n"
                ),
                model="stub",
            )

    llm = Counting(reply="", context_budget=40)
    result = Reviewer(config=Config(), repo=tmp_path, llm=llm).review(changeset)

    assert Counting.calls > 1, "a change this size should take several passes"
    assert len(result.findings) == Counting.calls, "every pass's findings must be kept"


def test_failed_chunk_marks_the_review_partial_without_losing_other_passes(tmp_path):
    from roborak.analysis.reviewer import Reviewer
    from roborak.core.config import Config
    from roborak.core.models import ReviewStatus
    from roborak.llm.client import LLMError, LLMResponse
    from tests.test_pipeline import StubLLM

    changeset = ChangeSet(files=[make_file(f"pkg{i}/f.py", 30) for i in range(5)])

    class SometimesFailing(StubLLM):
        calls = 0

        def complete(self, system, user):
            SometimesFailing.calls += 1
            if SometimesFailing.calls == 1:
                raise LLMError("one pass failed")
            return LLMResponse(text="findings: []", model="stub")

    result = Reviewer(
        config=Config(), repo=tmp_path, llm=SometimesFailing(reply="", context_budget=140)
    ).review(changeset)
    assert result.status is ReviewStatus.PARTIAL
    assert any("one pass failed" in error for error in result.errors)
    assert any(item.reason.value == "chunk_failed" for item in result.coverage)


def test_chunked_issue_uses_one_global_requirement_reducer(tmp_path):
    from roborak.analysis.reviewer import Reviewer
    from roborak.core.config import Config
    from roborak.core.models import Issue
    from roborak.core.severity import Kind
    from roborak.llm.client import LLMResponse
    from tests.test_pipeline import StubLLM

    changeset = ChangeSet(files=[make_file(f"pkg{i}/f.py", 30) for i in range(4)])

    class EvidenceLLM(StubLLM):
        def complete(self, system, user):
            if "checking whether a complete code change" in system:
                return LLMResponse(
                    text=(
                        "findings:\n"
                        "  - file: pkg0/f.py\n"
                        "    start_line: 1\n"
                        "    severity: major\n"
                        "    category: bug\n"
                        "    kind: requirement_gap\n"
                        "    body: The required timeout is absent.\n"
                    ),
                    model="stub",
                )
            return LLMResponse(
                text=(
                    "findings: []\nrequirement_evidence:\n"
                    "  - requirement: Preserve retries\n"
                    "    file: pkg0/f.py\n"
                    "    evidence: Retry handling remains present.\n"
                ),
                model="stub",
            )

    issue = Issue(provider="github", host="github.com", project="a/b", number=1, body="Timeout")
    result = Reviewer(
        config=Config(),
        repo=tmp_path,
        llm=EvidenceLLM(reply="", context_budget=140),
        issue=issue,
    ).review(changeset)
    assert [finding.kind for finding in result.findings] == [Kind.REQUIREMENT_GAP]
    assert result.usage[-1].purpose == "requirements"
