"""Blast-radius mapping: what a change reaches beyond the lines it touched.

The two claims this file exists to defend are the ones the feature is worthless
without: that a consumer in an unchanged file is found, and that *not* finding one
is never reported as proof the change is contained.
"""

from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path

import pytest

from roborak.context import ast_context, impact
from roborak.context.diff import whole_file_hunk
from roborak.core.config import ImpactConfig
from roborak.core.models import (
    BoundaryKind,
    ChangedFile,
    ChangeSet,
    ConsumerRelation,
    ImpactStatus,
    Verification,
)

requires_tree_sitter = pytest.mark.skipif(
    not ast_context.available(), reason="tree-sitter not installed"
)


def git(repo: Path, *args: str) -> None:
    subprocess.run(("git", *args), cwd=repo, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A real git repository, because the search backend is a real git command."""
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.email", "t@example.com")
    git(tmp_path, "config", "user.name", "t")
    return tmp_path


def write(repo: Path, path: str, body: str) -> None:
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(textwrap.dedent(body), encoding="utf-8")


def commit(repo: Path) -> None:
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "seed")


def changed(repo: Path, path: str, *, origin: str = "local", head: str = "") -> ChangeSet:
    """A changeset whose only file is ``path``, treated as wholly added."""
    content = (repo / path).read_text(encoding="utf-8")
    file = ChangedFile(
        path=path,
        change_type="added",
        language="python" if path.endswith(".py") else None,
        new_content=content,
        hunks=whole_file_hunk(content),
    )
    return ChangeSet(files=[file], origin=origin, head_sha=head)  # type: ignore[arg-type]


def node_named(result, name: str):
    return next((node for node in result.nodes if node.name == name), None)


def trace(repo: Path, path: str, name: str, config: ImpactConfig | None = None):
    """Analyse a one-file change and hand back the boundary called ``name``."""
    return node_named(impact.analyse(changed(repo, path), repo, config or ImpactConfig()), name)


# --- finding consumers ------------------------------------------------------


@requires_tree_sitter
def test_finds_a_direct_caller_in_an_unchanged_file(repo):
    write(repo, "service.py", "def charge_card(amount, currency):\n    return amount\n")
    write(repo, "checkout.py", "from service import charge_card\n\ncharge_card(10, 'usd')\n")
    commit(repo)

    result = impact.analyse(changed(repo, "service.py"), repo, ImpactConfig())

    node = node_named(result, "charge_card")
    assert node is not None
    assert node.status is ImpactStatus.CONSUMERS_FOUND
    assert node.kind is BoundaryKind.SYMBOL
    assert {consumer.path for consumer in node.consumers} == {"checkout.py"}
    assert result.status is ImpactStatus.CONSUMERS_FOUND


@requires_tree_sitter
def test_an_import_is_labelled_as_one(repo):
    """A re-export carries the name onward; it is not the same as calling it."""
    write(repo, "service.py", "def charge_card(amount):\n    return amount\n")
    write(repo, "api.py", "from service import charge_card\n")
    commit(repo)

    node = trace(repo, "service.py", "charge_card")
    assert node is not None
    assert node.consumers[0].relation is ConsumerRelation.IMPORT


@requires_tree_sitter
def test_a_test_file_is_labelled_as_one(repo):
    write(repo, "service.py", "def charge_card(amount):\n    return amount\n")
    write(repo, "tests/test_service.py", "from service import charge_card\n\ncharge_card(1)\n")
    commit(repo)

    node = trace(repo, "service.py", "charge_card")
    assert node is not None
    assert all(c.relation is ConsumerRelation.TEST for c in node.consumers)


@requires_tree_sitter
def test_a_consumer_carries_the_lines_around_the_reference(repo):
    write(repo, "service.py", "def charge_card(amount):\n    return amount\n")
    write(repo, "checkout.py", "def pay():\n    return charge_card(10)\n")
    commit(repo)

    node = trace(repo, "service.py", "charge_card")
    assert node is not None
    assert "charge_card(10)" in node.consumers[0].snippet


# --- containment, and the refusal to claim it -------------------------------


@requires_tree_sitter
def test_an_unused_symbol_is_reported_as_contained(repo):
    write(repo, "service.py", "def charge_card(amount):\n    return amount\n")
    write(repo, "unrelated.py", "def other():\n    return 1\n")
    commit(repo)

    node = trace(repo, "service.py", "charge_card")
    assert node is not None
    assert node.status is ImpactStatus.CONTAINED
    assert node.verification is Verification.PARSED
    assert "outside it" in node.note  # the residual risk is still stated


@requires_tree_sitter
def test_containment_is_never_claimed_for_a_pattern_matched_contract(repo):
    """A route is a name two files agree on, not a symbol a parser identified."""
    write(repo, "routes.py", 'app.get("/widgets/new")\n')
    commit(repo)

    result = impact.analyse(changed(repo, "routes.py"), repo, ImpactConfig())
    node = node_named(result, "/widgets/new")
    assert node is not None
    assert node.kind is BoundaryKind.ROUTE
    assert node.status is ImpactStatus.NO_REFERENCES_FOUND
    assert "alias" in node.note


@requires_tree_sitter
def test_a_mention_in_a_string_is_not_a_consumer_and_blocks_containment(repo):
    """Dynamic-reference uncertainty: the name is there, a use of it is not."""
    write(repo, "service.py", "def charge_card(amount):\n    return amount\n")
    write(repo, "dispatch.py", 'handler = getattr(svc, "charge_card")\n')
    commit(repo)

    node = trace(repo, "service.py", "charge_card")
    assert node is not None
    assert node.consumers == []
    assert node.status is ImpactStatus.NO_REFERENCES_FOUND
    assert "string literals" in node.note


@requires_tree_sitter
def test_a_truncated_search_cannot_report_containment(repo, monkeypatch):
    write(repo, "service.py", "def charge_card(amount):\n    return amount\n")
    for n in range(3):
        write(repo, f"filler_{n}.py", "x = 1\n")
    commit(repo)
    monkeypatch.setattr(impact, "_git", lambda *a, **k: None)  # force the walk

    config = ImpactConfig(max_files_scanned=1)
    result = impact.analyse(changed(repo, "service.py"), repo, config)

    assert result.method == "walk"
    assert result.truncated
    assert any("max_files_scanned" in note for note in result.notes)
    node = node_named(result, "charge_card")
    assert node is not None and node.status is not ImpactStatus.CONTAINED


# --- non-symbol contracts ---------------------------------------------------


@requires_tree_sitter
@pytest.mark.parametrize(
    ("source", "name", "kind"),
    [
        ('token = os.getenv("BILLING_API_KEY")\n', "BILLING_API_KEY", BoundaryKind.ENV_VAR),
        ('app.post("/v2/refunds")\n', "/v2/refunds", BoundaryKind.ROUTE),
        ('bus.publish("order.settled", payload)\n', "order.settled", BoundaryKind.EVENT),
    ],
)
def test_recognises_non_symbol_contracts(repo, source, name, kind):
    write(repo, "boundary.py", source)
    commit(repo)

    node = node_named(impact.analyse(changed(repo, "boundary.py"), repo, ImpactConfig()), name)
    assert node is not None and node.kind is kind


def test_recognises_a_config_key(repo):
    write(repo, "settings.yaml", "retry_budget: 5\n")
    commit(repo)

    changeset = changed(repo, "settings.yaml")
    changeset.files[0].language = "yaml"
    node = node_named(impact.analyse(changeset, repo, ImpactConfig()), "retry_budget")
    assert node is not None and node.kind is BoundaryKind.CONFIG_KEY


@requires_tree_sitter
def test_recognises_a_schema_field_and_finds_who_reads_it(repo):
    write(
        repo,
        "models.py",
        """\
        class Invoice(BaseModel):
            settled_at: str
        """,
    )
    write(repo, "report.py", "def show(invoice):\n    return invoice.settled_at\n")
    commit(repo)

    node = trace(repo, "models.py", "settled_at")
    assert node is not None
    assert node.kind is BoundaryKind.SCHEMA_FIELD
    assert node.consumers[0].path == "report.py"


@requires_tree_sitter
def test_a_plain_annotation_outside_a_schema_class_is_not_a_field(repo):
    """``name: str`` is the most common line in Python; it is not a contract."""
    write(repo, "plain.py", "class Helper:\n    retry_budget: int = 3\n")
    commit(repo)

    result = impact.analyse(changed(repo, "plain.py"), repo, ImpactConfig())
    assert node_named(result, "retry_budget") is None


@requires_tree_sitter
def test_recognises_an_exported_constant(repo):
    write(repo, "limits.py", "MAX_RETRY_BUDGET = 5\n")
    write(repo, "client.py", "from limits import MAX_RETRY_BUDGET\n")
    commit(repo)

    node = trace(repo, "limits.py", "MAX_RETRY_BUDGET")
    assert node is not None
    assert node.kind is BoundaryKind.EXPORT
    assert node.consumers[0].path == "client.py"


# --- bounds -----------------------------------------------------------------


@requires_tree_sitter
def test_the_node_budget_truncates_and_says_so(repo):
    body = "".join(f"def handler_{n}(x):\n    return x\n\n\n" for n in range(6))
    write(repo, "handlers.py", body)
    commit(repo)

    result = impact.analyse(changed(repo, "handlers.py"), repo, ImpactConfig(max_nodes=2))
    assert len(result.nodes) == 2
    assert result.truncated
    assert any("max_nodes" in note for note in result.notes)


@requires_tree_sitter
def test_the_consumer_budget_truncates_and_says_so(repo):
    write(repo, "service.py", "def charge_card(amount):\n    return amount\n")
    for n in range(5):
        write(repo, f"caller_{n}.py", "def go():\n    return charge_card(1)\n")
    commit(repo)

    config = ImpactConfig(max_consumers_per_node=2)
    node = trace(repo, "service.py", "charge_card", config)
    assert node is not None
    assert len(node.consumers) == 2
    assert node.truncated and "references found" in node.note


@requires_tree_sitter
def test_the_token_budget_trims_the_map(repo):
    write(repo, "service.py", "def charge_card(amount):\n    return amount\n")
    write(repo, "checkout.py", "def pay():\n    return charge_card(10)\n")
    commit(repo)

    result = impact.analyse(changed(repo, "service.py"), repo, ImpactConfig(token_budget=1))
    assert result.truncated
    assert any("budget" in note for note in result.notes)


# --- degradation ------------------------------------------------------------


def test_a_directory_with_no_repository_is_not_applicable(tmp_path, monkeypatch):
    """Every file is under review, so there is no unchanged consumer to find."""
    write(tmp_path, "service.py", "def charge_card(amount):\n    return amount\n")

    def explode(*args, **kwargs):
        raise AssertionError("a paths review must not shell out to git")

    monkeypatch.setattr(subprocess, "run", explode)
    changeset = changed(tmp_path, "service.py", origin="paths")
    changeset.omitted_files.append("huge.bin")

    result = impact.analyse(changeset, tmp_path, ImpactConfig())
    assert result.status is ImpactStatus.NOT_APPLICABLE
    assert result.nodes == []
    assert "under review" in result.notes[0]
    assert "1 file(s)" in result.notes[0]


def test_a_forge_change_without_a_matching_checkout_is_unavailable(repo):
    write(repo, "service.py", "def charge_card(amount):\n    return amount\n")
    commit(repo)

    changeset = changed(repo, "service.py", origin="github", head="0" * 40)
    result = impact.analyse(changeset, repo, ImpactConfig())

    assert result.status is ImpactStatus.UNAVAILABLE
    assert result.nodes == []
    assert "no checkout to search" in result.notes[0]


@requires_tree_sitter
def test_a_forge_change_whose_head_is_checked_out_is_limited(repo):
    write(repo, "service.py", "def charge_card(amount):\n    return amount\n")
    write(repo, "checkout.py", "def pay():\n    return charge_card(1)\n")
    commit(repo)
    head = subprocess.run(
        ("git", "rev-parse", "HEAD"), cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()

    changeset = changed(repo, "service.py", origin="gitlab", head=head)
    result = impact.analyse(changeset, repo, ImpactConfig())

    assert result.status is ImpactStatus.LIMITED
    assert node_named(result, "charge_card").consumers
    assert "may not hold exactly the code under review" in result.notes[0]


@requires_tree_sitter
def test_an_unusable_git_falls_back_to_walking_the_directory(repo, monkeypatch):
    write(repo, "service.py", "def charge_card(amount):\n    return amount\n")
    write(repo, "checkout.py", "def pay():\n    return charge_card(1)\n")
    commit(repo)

    real = subprocess.run

    def no_git(args, *rest, **kwargs):
        if args and args[0] == "git":
            raise OSError("git not found")
        return real(args, *rest, **kwargs)

    monkeypatch.setattr(subprocess, "run", no_git)
    result = impact.analyse(changed(repo, "service.py"), repo, ImpactConfig())

    assert result.method == "walk"
    assert node_named(result, "charge_card").consumers[0].path == "checkout.py"
    assert any("git grep" in note for note in result.notes)


@requires_tree_sitter
def test_an_untracked_consumer_in_a_repo_with_no_commits_is_found(repo):
    """Nothing is committed, so only ``--untracked`` can see the caller."""
    write(repo, "service.py", "def charge_card(amount):\n    return amount\n")
    write(repo, "checkout.py", "def pay():\n    return charge_card(1)\n")

    node = trace(repo, "service.py", "charge_card")
    assert node is not None
    assert node.consumers[0].path == "checkout.py"


def test_a_language_with_no_grammar_reports_unsupported(repo, monkeypatch):
    write(repo, "service.py", "def charge_card(amount):\n    return amount\n")
    commit(repo)
    monkeypatch.setattr(ast_context, "parse", lambda *a, **k: None)

    result = impact.analyse(changed(repo, "service.py"), repo, ImpactConfig())
    assert result.status is ImpactStatus.UNSUPPORTED
    assert "tree-sitter" in result.notes[0]


def test_prose_is_never_a_consumer(repo):
    """A changelog that mentions a function does not call it."""
    write(repo, "service.py", "def charge_card(amount):\n    return amount\n")
    write(repo, "CHANGELOG.md", "- charge_card now takes a currency\n")
    commit(repo)

    result = impact.analyse(changed(repo, "service.py"), repo, ImpactConfig())
    paths = {c.path for node in result.nodes for c in node.consumers}
    assert "CHANGELOG.md" not in paths


@requires_tree_sitter
def test_call_sites_win_the_consumer_budget_over_config_matches(repo):
    """``.github`` sorts before ``src``; relevance must beat alphabetical order."""
    write(repo, "service.py", "def charge_card(amount):\n    return amount\n")
    write(repo, ".github/ISSUE_TEMPLATE/bug.yml", "description: charge_card is broken\n")
    write(repo, "src/checkout.py", "def pay():\n    return charge_card(1)\n")
    commit(repo)

    node = trace(repo, "service.py", "charge_card", ImpactConfig(max_consumers_per_node=1))
    assert node is not None
    assert node.consumers[0].path == "src/checkout.py"
