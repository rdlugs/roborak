"""AST context and chunking."""

from __future__ import annotations

import textwrap

import pytest

from roborak.context import ast_context
from roborak.context.chunker import (
    MAX_CHUNKS,
    MAX_CONTRACT_CONTEXTS,
    chunk,
    needs_chunking,
    plan_chunks,
)
from roborak.context.diff import parse_diff
from roborak.core.models import (
    ChangedFile,
    ChangeSet,
    Consumer,
    Hunk,
    ImpactMap,
    ImpactNode,
    ReviewRole,
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


def test_tree_sitter_is_a_required_dependency():
    """A required dependency, so its absence is a failure and never a skip.

    Everything below silently proves nothing if the parser is not installed, and a
    green run that never touched the AST path is worse than a red one.
    """
    assert ast_context.available()


def test_finds_the_innermost_symbol():
    """The function, not the class that contains it."""
    file = ChangedFile(path="svc.py", language="python", new_content=PYTHON_SRC)
    span = ast_context.enclosing_symbol(file, hunk_at(9))
    assert span is not None
    assert span.name == "run"
    assert span.kind == "function_definition"
    assert span.start_line == 8


def test_finds_a_module_level_function():
    """Find a function declared directly at module scope."""
    file = ChangedFile(path="svc.py", language="python", new_content=PYTHON_SRC)
    span = ast_context.enclosing_symbol(file, hunk_at(15))
    assert span is not None and span.name == "helper"


def test_no_symbol_at_module_level():
    """Do not invent a symbol for a hunk at module scope."""
    file = ChangedFile(path="svc.py", language="python", new_content=PYTHON_SRC)
    assert ast_context.enclosing_symbol(file, hunk_at(1)) is None


def test_a_hunk_spanning_two_functions_falls_back_to_the_class():
    """Use the class when a hunk crosses multiple methods."""
    file = ChangedFile(path="svc.py", language="python", new_content=PYTHON_SRC)
    span = ast_context.enclosing_symbol(
        file, Hunk(old_start=5, old_lines=7, new_start=5, new_lines=7, content="")
    )
    assert span is not None and span.name == "Service"


def test_symbol_context_is_a_readable_note():
    """Render the enclosing symbol as a concise prompt note."""
    file = ChangedFile(path="svc.py", language="python", new_content=PYTHON_SRC)
    assert ast_context.symbol_context(file, hunk_at(9)) == "within function `run` (lines 8-11)"


def test_an_oversized_symbol_is_not_reported():
    """Naming a 500-line function does not help; it just costs tokens."""
    big = "def enormous():\n" + "\n".join(f"    x{i} = {i}" for i in range(300))
    file = ChangedFile(path="big.py", language="python", new_content=big)
    assert ast_context.enclosing_symbol(file, hunk_at(50)) is None


@pytest.mark.parametrize(
    ("language", "source", "line", "expected"),
    [
        ("javascript", "function outer() {\n  const a = 1;\n  return a;\n}\n", 2, "outer"),
        ("go", "package m\n\nfunc Run(x int) int {\n\treturn x\n}\n", 4, "Run"),
        ("rust", "fn compute(x: i32) -> i32 {\n    x + 1\n}\n", 2, "compute"),
    ],
)
def test_other_languages(language, source, line, expected):
    """Resolve symbols with bundled non-Python grammars."""
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


def test_a_syntactically_broken_file_does_not_raise():
    """Mid-review, half-written code is normal."""
    file = ChangedFile(path="a.py", language="python", new_content="def broken(:\n  ???\n")
    assert ast_context.symbol_context(file, hunk_at(1)) == ""


def test_an_unknown_language_is_survivable():
    """Ignore a file whose language has no bundled grammar."""
    file = ChangedFile(path="a.zzz", language="klingon", new_content="whatever")
    assert ast_context.enclosing_symbol(file, hunk_at(1)) is None


def make_file(path: str, lines: int, language: str = "python") -> ChangedFile:
    body = "\n".join(f"+line {i}" for i in range(lines))
    hunk = Hunk(old_start=1, old_lines=1, new_start=1, new_lines=lines, content=body)
    return ChangedFile(path=path, language=language, hunks=[hunk])


def make_text_file(path: str, text: str, language: str = "python") -> ChangedFile:
    lines = text.splitlines()
    body = "\n".join(f"+{line}" for line in lines)
    hunk = Hunk(
        old_start=1,
        old_lines=0,
        new_start=1,
        new_lines=len(lines),
        content=body,
        added_lines=set(range(1, len(lines) + 1)),
    )
    return ChangedFile(
        path=path,
        language=language,
        hunks=[hunk],
        new_content=text,
        change_type="added",
    )


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


def test_contracts_and_late_migrations_are_planned_before_leaf_code():
    files = [
        make_text_file("aaa/leaf.py", "def helper():\n    return 1"),
        make_text_file(
            "zzz/migrations/004_add_owner.sql", "ALTER TABLE jobs ADD owner TEXT", "sql"
        ),
        make_text_file("public/api.py", "def create_job(owner):\n    return owner"),
    ]
    plan = plan_chunks(ChangeSet(files=files), 25, count, render)
    assert [file.path for file in plan.review.files] == [
        "public/api.py",
        "zzz/migrations/004_add_owner.sql",
        "aaa/leaf.py",
    ]
    assert [file.role for file in plan.review.files] == [
        ReviewRole.CONTRACT,
        ReviewRole.SCHEMA_CONFIG,
        ReviewRole.IMPLEMENTATION,
    ]


def test_contract_and_changed_consumer_are_co_located_across_directories():
    contract = make_text_file("api/types.py", "class Job:\n    owner: str")
    consumer = make_text_file("workers/runner.py", "from api.types import Job\njob = Job()")
    impact = ImpactMap(
        nodes=[
            ImpactNode(
                name="Job",
                file=contract.path,
                consumers=[Consumer(path="unchanged.py", line=1)],
            )
        ]
    )
    budget = count(render(contract)) + count(render(consumer)) + 5
    plan = plan_chunks(ChangeSet(files=[consumer, contract]), budget, count, render, impact=impact)
    assert [file.path for file in plan.chunks[0].files] == [contract.path, consumer.path]
    assert plan.review.files[1].role is ReviewRole.CONSUMER
    assert [(item.path, item.name) for item in plan.contracts] == [(contract.path, "Job")]


def test_related_test_follows_its_implementation_when_the_pair_fits():
    implementation = make_text_file("src/service.py", "def charge():\n    return True")
    test = make_text_file(
        "tests/test_service.py", "from src.service import charge\nassert charge()"
    )
    budget = count(render(implementation)) + count(render(test)) + 5
    plan = plan_chunks(ChangeSet(files=[test, implementation]), budget, count, render)
    assert [file.path for file in plan.chunks[0].files] == [implementation.path, test.path]


def test_relative_import_groups_a_consumer_with_the_module_it_imports():
    """A relative specifier names a path, not a dotted module."""
    module = make_text_file("web/api.js", "export function load() {\n  return 1;\n}", "javascript")
    consumer = make_text_file(
        "ui/panel.js", "import { load } from '../web/api';\nload();", "javascript"
    )
    budget = count(render(module)) + count(render(consumer)) + 5
    plan = plan_chunks(ChangeSet(files=[consumer, module]), budget, count, render)
    assert [file.path for file in plan.chunks[0].files] == [module.path, consumer.path]
    roles = {file.path: file.role for file in plan.review.files}
    assert roles[consumer.path] is ReviewRole.CONSUMER


def test_semantic_order_is_deterministic_for_reversed_input():
    files = [
        make_text_file("docs/generated.md", "Generated reference", "markdown"),
        make_text_file("src/worker.py", "def run():\n    pass"),
        make_text_file("config/app.yaml", "timeout: 5", "yaml"),
    ]
    forward = plan_chunks(ChangeSet(files=files), 20, count, render)
    reverse = plan_chunks(ChangeSet(files=list(reversed(files))), 20, count, render)
    assert [file.path for file in forward.review.files] == [
        file.path for file in reverse.review.files
    ]


def test_low_signal_files_are_omitted_before_boundaries_at_the_pass_cap():
    files = [make_text_file("public/api.py", "def public_call():\n    return 1")]
    files.extend(make_file(f"generated/f{i:02d}.md", 100, "markdown") for i in range(20))
    plan = plan_chunks(ChangeSet(files=files), 25, count, render)
    assert len(plan.chunks) == MAX_CHUNKS
    assert plan.review.files[0].path == "public/api.py"
    assert plan.review.files[0].reviewed
    assert plan.review.omitted_roles[ReviewRole.LOW_SIGNAL] > 0


def test_uncertain_classification_falls_back_to_implementation():
    plan = plan_chunks(
        ChangeSet(
            files=[make_text_file("odd/place/widget.zzz", "something unfamiliar", "klingon")]
        ),
        100,
        count,
        render,
    )
    assert plan.review.files[0].role is ReviewRole.IMPLEMENTATION


def test_contract_metadata_is_bounded_and_not_added_as_primary_diff():
    files = [
        make_text_file(f"public/api{i}.py", f"def call_{i}():\n    return {i}")
        for i in range(MAX_CONTRACT_CONTEXTS + 5)
    ]
    plan = plan_chunks(ChangeSet(files=files), 30, count, render)
    assert len(plan.contracts) == MAX_CONTRACT_CONTEXTS
    primary = [file.path for piece in plan.chunks for file in piece.files]
    assert set(primary).issubset({file.path for file in files})


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
            if "reconcile evidence collected" in system:
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
    assert result.usage[-1].purpose == "reconciliation"


def test_global_reconciliation_can_report_a_cross_chunk_contract_mismatch(tmp_path, monkeypatch):
    from roborak.analysis.reviewer import Reviewer
    from roborak.context import impact as impact_module
    from roborak.core.config import Config
    from roborak.llm.client import LLMResponse
    from tests.test_pipeline import StubLLM

    contract = make_text_file("public/api.py", "def load(limit: int):\n    return limit")
    consumer = make_text_file("zzz/client.py", "from public.api import load\nload()")
    mapped = ImpactMap(
        nodes=[
            ImpactNode(
                name="load",
                file=contract.path,
                consumers=[Consumer(path="old_client.py", line=1)],
            )
        ]
    )
    monkeypatch.setattr(impact_module, "analyse", lambda *args: mapped)

    class Reconciling(StubLLM):
        reducer_prompt = ""

        def complete(self, system, user):
            if "reconcile evidence collected" in system:
                Reconciling.reducer_prompt = user
                return LLMResponse(
                    text=(
                        "findings:\n"
                        "  - file: public/api.py\n"
                        "    start_line: 1\n"
                        "    severity: major\n"
                        "    category: bug\n"
                        "    body: The required argument breaks zzz/client.py.\n"
                        "    evidence: contract\n"
                        "    evidence_note: The caller still invokes load with no argument.\n"
                        "    evidence_files: [zzz/client.py]\n"
                    ),
                    model="stub",
                )
            path = "public/api.py" if "public/api.py" in user else "zzz/client.py"
            return LLMResponse(
                text=(
                    "findings: []\n"
                    "compatibility_evidence:\n"
                    "  - contract: load\n"
                    "    contract_file: public/api.py\n"
                    f"    file: {path}\n"
                    "    status: incompatible\n"
                    "    evidence: The caller supplies no limit.\n"
                ),
                model="stub",
            )

    result = Reviewer(
        config=Config(), repo=tmp_path, llm=Reconciling(reply="", context_budget=12)
    ).review(ChangeSet(files=[consumer, contract]))
    assert [finding.file for finding in result.findings] == ["public/api.py"]
    assert result.usage[-1].purpose == "reconciliation"
    # The reducer resolves evidence against the contract catalogue by path, so the
    # path each pass named has to survive into its prompt.
    assert "contract_file: public/api.py" in Reconciling.reducer_prompt


def test_failed_reconciliation_marks_an_otherwise_complete_review_partial(tmp_path):
    from roborak.analysis.reviewer import Reviewer
    from roborak.core.config import Config
    from roborak.core.models import Issue, ReviewStatus
    from roborak.llm.client import LLMError, LLMResponse
    from tests.test_pipeline import StubLLM

    class FailingReducer(StubLLM):
        def complete(self, system, user):
            if "reconcile evidence collected" in system:
                raise LLMError("reducer unavailable")
            return LLMResponse(text="findings: []", model="stub")

    result = Reviewer(
        config=Config(),
        repo=tmp_path,
        llm=FailingReducer(reply="", context_budget=140),
        issue=Issue(provider="github", host="github.com", project="a/b", number=1),
    ).review(ChangeSet(files=[make_file(f"pkg{i}/f.py", 30) for i in range(4)]))

    assert result.status is ReviewStatus.PARTIAL
    assert result.errors == ["reconciliation failed: reducer unavailable"]


def test_verbose_evidence_is_trimmed_to_fit_the_reconciliation_budget(tmp_path):
    from roborak.analysis.reviewer import Reviewer
    from roborak.core.config import Config
    from roborak.core.models import Issue, ReviewStatus
    from roborak.llm.client import LLMResponse
    from tests.test_pipeline import StubLLM

    budget = 4000
    flood = "findings: []\nrequirement_evidence:\n" + "".join(
        f"  - requirement: Requirement {index}\n"
        f"    file: pkg0/f.py\n"
        f"    evidence: {'verbose ' * 40}\n"
        for index in range(30)
    )

    class Flooding(StubLLM):
        reconciliation_tokens = 0

        def complete(self, system, user):
            if "reconcile evidence collected" in system:
                Flooding.reconciliation_tokens = self.count_tokens(f"{system}\n{user}")
                return LLMResponse(text="findings: []", model="stub")
            return LLMResponse(text=flood, model="stub")

    issue = Issue(provider="github", host="github.com", project="a/b", number=1, body="Timeout")
    result = Reviewer(
        config=Config(),
        repo=tmp_path,
        llm=Flooding(reply="", context_budget=budget),
        issue=issue,
    ).review(ChangeSet(files=[make_file(f"pkg{i}/f.py", 60) for i in range(6)]))

    assert result.status is ReviewStatus.COMPLETE
    assert 0 < Flooding.reconciliation_tokens <= budget


def test_reconciliation_fits_the_budget_when_contracts_alone_overflow_it(tmp_path):
    """Evidence is not the only thing that can push the reducer over its budget, and
    an over-budget reducer call turns a complete review into a PARTIAL one."""
    from roborak.analysis.reviewer import Reviewer
    from roborak.context.chunker import ContractContext
    from roborak.core.config import Config
    from tests.test_pipeline import StubLLM

    budget = 1000
    llm = StubLLM(reply="", context_budget=budget)
    contracts = [
        ContractContext(
            path=f"services/subsystem_{index}/public_interface.py",
            name=f"handler_{index}",
            kind="function",
            line=index + 1,
            summary=f"def handler_{index}(payload: Payload) -> Response",
        )
        for index in range(MAX_CONTRACT_CONTEXTS)
    ]
    files = [f"services/subsystem_{index}/public_interface.py" for index in range(200)]

    prompt = Reviewer(config=Config(), repo=tmp_path, llm=llm)._reconciliation_prompt(
        requirement_evidence=[],
        compatibility_evidence=[],
        contracts=contracts,
        files=files,
    )

    assert llm.count_tokens(f"{prompt.system}\n{prompt.user}") <= budget


def test_carried_contracts_do_not_compress_a_file_the_plan_promised_to_review(tmp_path):
    """The plan has to leave room for the contracts later passes are handed.

    They are not rendered when the budget is measured, so a plan that ignores them
    fills every pass to the brim and the carried section pushes it back over --
    compressing away files the plan had just promised to review.
    """
    from roborak.analysis.reviewer import Reviewer
    from roborak.core.config import Config
    from roborak.core.models import ReviewStatus
    from roborak.llm.client import LLMResponse
    from tests.test_pipeline import StubLLM

    budget = 7500
    contracts = [
        make_text_file(
            f"public/api{index}.py",
            "\n".join(
                f"def load_{i}(limit: int, retries: int, timeout: float) -> int:\n"
                "    return limit + retries"
                for i in range(20)
            ),
        )
        for index in range(12)
    ]
    changeset = ChangeSet(files=[*contracts, *(make_file(f"pkg{i}/f.py", 8) for i in range(90))])

    sizes: list[int] = []

    class Recording(StubLLM):
        def complete(self, system, user):
            sizes.append(self.count_tokens(f"{system}\n{user}"))
            return LLMResponse(text="findings: []", model="stub")

    result = Reviewer(
        config=Config(), repo=tmp_path, llm=Recording(reply="", context_budget=budget)
    ).review(changeset)

    assert max(sizes) <= budget
    assert result.status is ReviewStatus.COMPLETE
    assert [error for error in result.errors if "context budget omitted" in error] == []
    assert result.review_plan is not None
    assert all(planned.reviewed for planned in result.review_plan.files)


def test_a_single_pass_budget_reserves_nothing_for_contracts_it_will_never_carry(tmp_path):
    from roborak.analysis.reviewer import Reviewer
    from roborak.core.config import Config
    from tests.test_pipeline import StubLLM

    reviewer = Reviewer(config=Config(), repo=tmp_path, llm=StubLLM(reply="", context_budget=40000))
    contract = make_text_file("public/api.py", "def load(limit: int):\n    return limit")
    changeset = ChangeSet(files=[contract, make_file("pkg/f.py", 20)])

    whole = reviewer._diff_budget(changeset)
    chunked = reviewer._diff_budget(changeset, carries_contracts=True)
    assert chunked < whole

    without_contracts = ChangeSet(files=[make_file("pkg/f.py", 20)])
    assert reviewer._diff_budget(
        without_contracts, carries_contracts=True
    ) == reviewer._diff_budget(without_contracts)


def _split_changeset() -> ChangeSet:
    """A contract big enough to be fragmented across a chunk boundary."""
    contract = make_text_file(
        "public/api.py",
        "\n".join(
            f"def load_{i}(limit: int, retries: int, timeout: float) -> int:\n"
            "    return limit + retries"
            for i in range(60)
        ),
    )
    return ChangeSet(files=[contract, *(make_file(f"pkg{i}/f.py", 10) for i in range(6))])


def test_the_plan_reports_the_first_pass_that_reviewed_a_split_file():
    from roborak.context.chunker import plan_chunks

    changeset = _split_changeset()
    from roborak.analysis.reviewer import render_for_prompt

    plan = plan_chunks(changeset, 900, lambda text: len(text) // 4, render_for_prompt)

    spans = [
        index
        for index, piece in enumerate(plan.chunks, start=1)
        if "public/api.py" in {file.path for file in piece.files}
    ]
    assert len(spans) > 1, "the contract should be split across passes for this to mean anything"
    planned = next(file for file in plan.review.files if file.path == "public/api.py")
    assert planned.chunk == spans[0]


def test_a_contract_is_not_carried_into_a_pass_still_reviewing_it(tmp_path):
    """Carrying is keyed on the first pass, so the guard against self-carry matters.

    A split file stays primary diff in the pass holding its later fragments. Handed
    its own summary there, under an instruction not to report on an earlier pass's
    file, the model would be told to stay off the very lines it is reviewing.
    """
    from roborak.analysis.reviewer import Reviewer
    from roborak.core.config import Config
    from roborak.llm.client import LLMResponse
    from tests.test_pipeline import StubLLM

    section = "## Contracts established in earlier review passes"
    both: list[int] = []

    class Recording(StubLLM):
        def complete(self, system, user):
            if "reconcile evidence collected" in system:
                return LLMResponse(text="findings: []", model="stub")
            diff = user.split("## Diff to review")[-1]
            carried = (
                section in user
                and "public/api.py" in user.split(section)[1].split("## Diff to review")[0]
            )
            both.append(carried and "public/api.py" in diff)
            return LLMResponse(text="findings: []", model="stub")

    Reviewer(config=Config(), repo=tmp_path, llm=Recording(reply="", context_budget=5000)).review(
        _split_changeset()
    )

    assert len(both) > 1, "the contract should span several passes for this to mean anything"
    assert not any(both)
