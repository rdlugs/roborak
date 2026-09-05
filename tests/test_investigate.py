"""The investigation stage: what it may read, what it refuses, and what it settles.

The execution boundary is tested against a real git repository and real files on
disk, because the whole value of the stage is that it read the code that is
actually there -- a test that stubbed the reading would prove nothing about it.

The safety property under test throughout is negative: a candidate the stage
cannot settle must come back exactly as it arrived. Most tests below are a way of
asking "and what happens when this goes wrong?" and expecting nothing to happen.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

from roborak.analysis import validator
from roborak.analysis.reviewer import Reviewer
from roborak.core.config import Config, InvestigateConfig
from roborak.core.models import (
    ChangedFile,
    ChangeSet,
    Finding,
    Hunk,
    InvestigationOutcome,
    InvestigationReport,
    InvestigationStatus,
    ReviewResult,
)
from roborak.core.severity import Category, Evidence, Kind, Severity
from roborak.investigate import availability, tools
from roborak.investigate.runner import investigate, select
from roborak.render import json_out, markdown, terminal


def git(repo: Path, *args: str) -> None:
    subprocess.run(("git", *args), cwd=repo, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A real git repository, because the search backend is a real git command."""
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "config", "user.email", "t@example.com")
    git(tmp_path, "config", "user.name", "Test")
    (tmp_path / "app.py").write_text(
        "def charge(amount):\n    return amount * 2\n\n\ndef caller():\n    return charge(None)\n"
    )
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-qm", "init")
    return tmp_path


def cfg(**overrides: object) -> InvestigateConfig:
    return InvestigateConfig.model_validate(overrides)


def changeset(origin: str = "local", **kwargs: object) -> ChangeSet:
    hunk = Hunk(
        old_start=1,
        old_lines=1,
        new_start=1,
        new_lines=2,
        header="@@ -1 +1,2 @@",
        content="+def charge(amount):",
        added_lines={1, 2},
    )
    return ChangeSet(
        files=[ChangedFile(path="app.py", hunks=[hunk])],
        origin=origin,  # type: ignore[arg-type]
        **kwargs,  # type: ignore[arg-type]
    )


def finding(**overrides: object) -> Finding:
    defaults: dict[str, object] = {
        "file": "app.py",
        "start_line": 2,
        "end_line": 2,
        "severity": Severity.CRITICAL,
        "category": Category.BUG,
        "kind": Kind.POTENTIAL_ISSUE,
        "title": "charge doubles a None",
        "body": "charge is called with None.",
        "confidence": 0.9,
    }
    return Finding.model_validate(defaults | overrides)


def replies(*texts: str):
    """A ``complete`` that answers each round in turn, then repeats the last."""
    seen: list[tuple[str, str]] = []

    def complete(system: str, user: str) -> str:
        seen.append((system, user))
        return texts[min(len(seen) - 1, len(texts) - 1)]

    complete.seen = seen  # type: ignore[attr-defined]
    return complete


CONFIRM = """
decisions:
  - candidate: c1
    disposition: confirm
    rationale: caller() passes None straight into the multiplication.
    evidence: execution_path
    evidence_note: caller() at app.py:6 calls charge(None); the multiply raises TypeError.
"""

DROP = """
decisions:
  - candidate: c1
    disposition: drop
    rationale: the caller guards the value before calling.
"""


# --- the execution boundary --------------------------------------------------


@pytest.mark.parametrize(
    "candidate",
    ["../outside.py", "../../etc/passwd", "/etc/passwd", "a/../../../etc/passwd", "", " app.py"],
)
def test_paths_outside_the_repository_are_refused(repo: Path, candidate: str):
    """Containment is proved before any I/O, so traversal never reaches a read."""
    assert tools.resolve_in_repo(repo, candidate) is None


def test_a_symlink_pointing_out_of_the_tree_is_refused(repo: Path, tmp_path: Path):
    """Resolution follows links, so an escaping link fails the same check as `..`."""
    secret = tmp_path.parent / "secret.txt"
    secret.write_text("token")
    (repo / "link.py").symlink_to(secret)

    assert tools.resolve_in_repo(repo, "link.py") is None
    result = tools.read_lines(repo, "link.py", start=1, end=5, config=cfg())
    assert not result.ok
    assert "outside the repository" in result.error


def test_a_contained_path_resolves(repo: Path):
    assert tools.resolve_in_repo(repo, "app.py") == (repo / "app.py").resolve()


def test_read_lines_numbers_the_window_it_returns(repo: Path):
    result = tools.read_lines(repo, "app.py", start=5, end=6, config=cfg())
    assert result.ok
    assert result.text == "5: def caller():\n6:     return charge(None)"


def test_read_lines_truncates_and_says_so(repo: Path):
    result = tools.read_lines(repo, "app.py", start=1, end=6, config=cfg(max_lines_per_read=2))
    assert result.truncated
    assert result.text.splitlines() == ["1: def charge(amount):", "2:     return amount * 2"]


def test_read_lines_past_the_end_is_an_error_not_an_empty_answer(repo: Path):
    """An empty result would read as "there is nothing there", which is a different claim."""
    result = tools.read_lines(repo, "app.py", start=999, end=1000, config=cfg())
    assert not result.ok
    assert "past the end" in result.error


def test_search_finds_a_caller(repo: Path):
    result = tools.search(repo, "charge", regex=False, path_prefix="", config=cfg())
    assert result.ok
    assert "app.py:6" in result.text


def test_search_caps_its_results(repo: Path):
    (repo / "many.py").write_text("charge\n" * 50)
    result = tools.search(
        repo, "charge", regex=False, path_prefix="", config=cfg(max_search_results=3)
    )
    assert result.truncated
    assert len(result.text.splitlines()) == 3


def test_search_bounds_its_output(repo: Path):
    result = tools.search(
        repo, "charge", regex=False, path_prefix="", config=cfg(max_output_chars=10)
    )
    assert result.truncated
    assert len(result.text) == 10


def test_search_refuses_a_path_outside_the_repository(repo: Path):
    result = tools.search(repo, "charge", regex=False, path_prefix="../", config=cfg())
    assert not result.ok


def test_search_rejects_a_broken_regular_expression(repo: Path):
    result = tools.search(repo, "charge(", regex=True, path_prefix="", config=cfg())
    assert not result.ok
    assert "invalid regular expression" in result.error


def test_search_rejects_an_oversized_pattern(repo: Path):
    result = tools.search(repo, "x" * 500, regex=False, path_prefix="", config=cfg())
    assert not result.ok


def test_search_finding_nothing_is_an_answer(repo: Path):
    result = tools.search(repo, "nowhere_at_all", regex=False, path_prefix="", config=cfg())
    assert result.ok
    assert result.text == ""


def test_show_diff_comes_from_the_changeset_not_the_tree(repo: Path):
    result = tools.show_diff(changeset(), "app.py", config=cfg())
    assert result.ok
    assert "@@ -1 +1,2 @@" in result.text


def test_show_diff_names_the_files_it_does_have(repo: Path):
    result = tools.show_diff(changeset(), "other.py", config=cfg())
    assert not result.ok
    assert "app.py" in result.error


# --- the checkout guard ------------------------------------------------------


def test_a_local_change_may_read_the_working_tree(repo: Path):
    access = availability.resolve(changeset(), repo)
    assert access.reads_working_tree and access.searches


def test_a_forge_change_matching_the_checkout_may_read_it(repo: Path):
    head = subprocess.run(
        ("git", "rev-parse", "HEAD"), cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()
    access = availability.resolve(changeset(origin="github", head_sha=head), repo)
    assert access.reads_working_tree and access.searches


def test_a_forge_change_against_a_different_checkout_never_reads_it(repo: Path):
    """The expensive mistake this stage could make is confirming a finding against
    code that is not in the merge request."""
    access = availability.resolve(changeset(origin="github", head_sha="b" * 40), repo)
    assert not access.reads_working_tree
    assert not access.searches
    assert "does not match" not in "".join(access.notes) or access.notes


def test_a_dirty_checkout_is_not_trusted_even_at_the_right_commit(repo: Path):
    head = subprocess.run(
        ("git", "rev-parse", "HEAD"), cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()
    (repo / "app.py").write_text("something else entirely\n")

    access = availability.resolve(changeset(origin="github", head_sha=head), repo)
    assert not access.reads_working_tree
    assert "uncommitted" in "".join(access.notes)


def test_a_forge_change_with_no_head_sha_and_no_content_is_unavailable(repo: Path):
    access = availability.resolve(changeset(origin="github"), repo)
    assert not access.usable


def forge_change(head: str = "b" * 40) -> ChangeSet:
    """A forge change that carries its own file content, as a real one does."""
    return ChangeSet(
        files=[
            ChangedFile(
                path="app.py",
                new_content="def charge(amount):\n    return amount * 2\n",
            )
        ],
        origin="github",
        head_sha=head,
    )


def test_a_mismatched_checkout_degrades_to_the_forge_content(repo: Path):
    """The change's own files came from the forge, so they can still be read."""
    access = availability.resolve(forge_change(), repo)
    assert access.reads_changeset
    assert not access.reads_working_tree
    assert not access.searches
    assert access.status is InvestigationStatus.PARTIAL


def test_the_degraded_mode_reads_forge_content_not_the_tree(repo: Path):
    """The file on disk says something different; the read must not return it."""
    (repo / "app.py").write_text("THIS IS ANOTHER BRANCH\n" * 5)
    complete = replies(
        "requests:\n  - tool: read_file\n    path: app.py\n    start: 1\n    end: 2\n",
        CONFIRM,
    )
    _, report = investigate([finding()], forge_change(), repo=repo, config=cfg(), complete=complete)

    assert "def charge" in report.operations[0].result
    assert "ANOTHER BRANCH" not in report.operations[0].result


def test_a_degraded_run_is_never_reported_as_complete(repo: Path):
    complete = replies(CONFIRM)
    _, report = investigate([finding()], forge_change(), repo=repo, config=cfg(), complete=complete)
    assert report.status is InvestigationStatus.PARTIAL


def test_the_degraded_mode_refuses_a_search(repo: Path):
    complete = replies("requests:\n  - tool: search\n    pattern: charge\n", CONFIRM)
    _, report = investigate([finding()], forge_change(), repo=repo, config=cfg(), complete=complete)
    assert report.operations[0].outcome is InvestigationOutcome.REFUSED
    assert "checkout" in report.operations[0].note


def test_the_degraded_mode_cannot_read_a_file_the_change_does_not_carry(repo: Path):
    complete = replies(
        "requests:\n  - tool: read_file\n    path: other.py\n    start: 1\n    end: 2\n",
        CONFIRM,
    )
    _, report = investigate([finding()], forge_change(), repo=repo, config=cfg(), complete=complete)
    assert report.operations[0].outcome is InvestigationOutcome.ERRORED
    assert "did not come with it" in report.operations[0].note


# --- symbol lookup -----------------------------------------------------------


def test_find_symbol_locates_a_declaration(repo: Path):
    complete = replies(
        "requests:\n  - tool: find_symbol\n    path: app.py\n    symbol: charge\n",
        CONFIRM,
    )
    _, report = investigate([finding()], changeset(), repo=repo, config=cfg(), complete=complete)

    operation = report.operations[0]
    assert operation.outcome is InvestigationOutcome.OK, operation.note
    assert "def charge" in operation.result
    assert operation.result.startswith("app.py:1")


def test_find_symbol_says_so_when_the_symbol_is_absent(repo: Path):
    complete = replies(
        "requests:\n  - tool: find_symbol\n    path: app.py\n    symbol: nowhere\n",
        CONFIRM,
    )
    _, report = investigate([finding()], changeset(), repo=repo, config=cfg(), complete=complete)
    assert report.operations[0].outcome is InvestigationOutcome.ERRORED


def test_find_symbol_refuses_a_path_outside_the_repository(repo: Path):
    complete = replies(
        "requests:\n  - tool: find_symbol\n    path: ../x.py\n    symbol: charge\n",
        CONFIRM,
    )
    _, report = investigate([finding()], changeset(), repo=repo, config=cfg(), complete=complete)
    assert report.operations[0].outcome is InvestigationOutcome.ERRORED


def test_show_diff_is_reachable_as_an_operation(repo: Path):
    complete = replies("requests:\n  - tool: show_diff\n    path: app.py\n", CONFIRM)
    _, report = investigate([finding()], changeset(), repo=repo, config=cfg(), complete=complete)
    assert report.operations[0].tool == "show_diff"
    assert "@@" in report.operations[0].result


def test_a_search_operation_runs_for_a_local_change(repo: Path):
    complete = replies(
        "requests:\n  - tool: search\n    pattern: charge\n    regex: 'false'\n", CONFIRM
    )
    _, report = investigate([finding()], changeset(), repo=repo, config=cfg(), complete=complete)
    assert report.operations[0].outcome is InvestigationOutcome.OK
    assert "app.py" in report.operations[0].result


# --- candidate selection -----------------------------------------------------


def test_selection_takes_the_findings_the_evidence_policy_would_demote():
    demotable = finding()
    minor = finding(severity=Severity.MINOR, title="nitpick")
    static = finding(source="static", tool="ruff", title="from a tool")

    chosen = select([demotable, minor, static], cfg())
    assert [f.title for f in chosen] == ["charge doubles a None"]
    assert validator.is_unproven_blocker(demotable)


def test_selection_is_capped():
    findings = [finding(title=f"f{i}") for i in range(10)]
    assert len(select(findings, cfg(max_candidates=3))) == 3


# --- confirm, revise, drop ---------------------------------------------------


def test_confirming_a_candidate_saves_it_from_demotion(repo: Path):
    """The point of the stage: evidence gathered here is what `enforce_evidence` reads."""
    candidate = finding()
    findings = [candidate]

    findings, report = investigate(
        findings, changeset(), repo=repo, config=cfg(), complete=replies(CONFIRM)
    )

    assert report.status is InvestigationStatus.COMPLETED
    assert candidate.evidence is Evidence.EXECUTION_PATH
    assert "caller()" in candidate.evidence_note

    kept = validator.enforce_evidence(findings)
    assert kept[0].severity is Severity.CRITICAL
    assert kept[0].kind is Kind.POTENTIAL_ISSUE


def test_an_unconfirmed_candidate_is_still_demoted(repo: Path):
    candidate = finding()
    findings = validator.enforce_evidence([candidate])
    assert findings[0].severity is Severity.MINOR
    assert findings[0].kind is Kind.VERIFICATION_NEEDED


def test_dropping_a_candidate_removes_it(repo: Path):
    findings = [finding()]
    findings, report = investigate(
        findings, changeset(), repo=repo, config=cfg(), complete=replies(DROP)
    )
    assert findings == []
    assert report.decisions[0].disposition == "drop"


def test_revising_a_candidate_moves_it(repo: Path):
    candidate = finding()
    _, report = investigate(
        [candidate],
        changeset(),
        repo=repo,
        config=cfg(),
        complete=replies(
            "decisions:\n"
            "  - candidate: c1\n"
            "    disposition: revise\n"
            "    severity: major\n"
            "    start_line: 6\n"
            "    title: charge is called with None\n"
            "    evidence: contract\n"
            "    evidence_note: charge documents an int parameter.\n"
        ),
    )
    assert candidate.start_line == 6
    assert candidate.severity is Severity.MAJOR
    assert candidate.title == "charge is called with None"
    assert report.decisions[0].disposition == "revise"


def test_a_decision_cannot_claim_a_tool_ran(repo: Path):
    """`static_tool` means an analyser said so. Nothing ran here."""
    candidate = finding()
    investigate(
        [candidate],
        changeset(),
        repo=repo,
        config=cfg(),
        complete=replies(
            "decisions:\n"
            "  - candidate: c1\n"
            "    disposition: confirm\n"
            "    evidence: static_tool\n"
            "    evidence_note: ruff said so\n"
        ),
    )
    assert candidate.evidence is Evidence.UNVERIFIED


def test_a_decision_for_an_unknown_candidate_is_discarded(repo: Path):
    candidate = finding()
    findings, report = investigate(
        [candidate],
        changeset(),
        repo=repo,
        config=cfg(),
        complete=replies(
            "decisions:\n  - candidate: c99\n    disposition: drop\n    rationale: nope\n"
        ),
    )
    assert findings == [candidate]
    assert report.decisions[0].disposition == "unresolved"
    assert report.status is InvestigationStatus.PARTIAL


# --- failure leaves candidates alone -----------------------------------------


def test_an_unparseable_reply_leaves_the_candidate_alone(repo: Path):
    candidate = finding()
    before = candidate.model_copy(deep=True)
    findings, report = investigate(
        [candidate], changeset(), repo=repo, config=cfg(), complete=replies("!!! not yaml {[")
    )
    assert findings == [candidate]
    assert candidate.severity is before.severity
    assert candidate.evidence is before.evidence
    assert report.unresolved == 1


def test_a_raising_model_leaves_the_candidate_alone(repo: Path):
    def explode(system: str, user: str) -> str:
        raise RuntimeError("provider is down")

    candidate = finding()
    findings, report = investigate(
        [candidate], changeset(), repo=repo, config=cfg(), complete=explode
    )
    assert findings == [candidate]
    assert report.status is InvestigationStatus.ERRORED
    assert candidate.evidence is Evidence.UNVERIFIED


def test_the_round_limit_leaves_the_candidate_unresolved(repo: Path):
    """A model that only ever asks for more must never be recorded as having decided."""
    asking = replies("requests:\n  - tool: read_file\n    path: app.py\n    start: 1\n    end: 6\n")
    candidate = finding()
    findings, report = investigate(
        [candidate], changeset(), repo=repo, config=cfg(max_rounds=2), complete=asking
    )
    assert findings == [candidate]
    assert report.rounds == 2
    assert report.unresolved == 1
    assert candidate.evidence is Evidence.UNVERIFIED


def test_an_exhausted_token_budget_stops_the_stage_asking(repo: Path):
    asking = replies("requests:\n  - tool: read_file\n    path: app.py\n    start: 1\n    end: 6\n")
    _, report = investigate(
        [finding()],
        changeset(),
        repo=repo,
        config=cfg(max_rounds=5, token_budget=1),
        complete=asking,
    )
    assert report.rounds < 5
    assert report.unresolved == 1


def test_a_remote_mismatch_leaves_every_candidate_unresolved(repo: Path):
    candidate = finding()
    findings, report = investigate(
        [candidate],
        changeset(origin="github", head_sha="b" * 40),
        repo=repo,
        config=cfg(),
        complete=replies(DROP),
    )
    assert findings == [candidate]
    assert report.status is InvestigationStatus.UNAVAILABLE
    assert report.decisions[0].disposition == "unresolved"


def test_no_candidates_means_no_model_call(repo: Path):
    calls = replies(CONFIRM)
    _, report = investigate(
        [finding(severity=Severity.MINOR)],
        changeset(),
        repo=repo,
        config=cfg(),
        complete=calls,
    )
    assert calls.seen == []  # type: ignore[attr-defined]
    assert report.status is InvestigationStatus.SKIPPED


# --- operations are recorded -------------------------------------------------


def test_requested_operations_and_their_results_are_recorded(repo: Path):
    complete = replies(
        "requests:\n  - tool: read_file\n    path: app.py\n    start: 5\n    end: 6\n",
        CONFIRM,
    )
    _, report = investigate([finding()], changeset(), repo=repo, config=cfg(), complete=complete)

    assert [op.tool for op in report.operations] == ["read_file"]
    assert report.operations[0].outcome is InvestigationOutcome.OK
    assert "caller" in report.operations[0].result
    assert report.rounds == 2


def test_a_refused_operation_is_recorded_rather_than_dropped(repo: Path):
    """A model that never learns its request was refused will keep making it."""
    complete = replies(
        "requests:\n  - tool: read_file\n    path: ../../etc/passwd\n    start: 1\n    end: 2\n",
        CONFIRM,
    )
    _, report = investigate([finding()], changeset(), repo=repo, config=cfg(), complete=complete)

    assert report.operations[0].outcome is InvestigationOutcome.ERRORED
    assert "outside the repository" in report.operations[0].note


def test_an_unknown_tool_never_reaches_the_repository(repo: Path):
    complete = replies("requests:\n  - tool: rm_rf\n    path: app.py\n", CONFIRM)
    _, report = investigate([finding()], changeset(), repo=repo, config=cfg(), complete=complete)
    assert report.operations == []


def test_the_file_limit_refuses_further_reads(repo: Path):
    (repo / "b.py").write_text("b = 1\n")
    (repo / "c.py").write_text("c = 1\n")
    complete = replies(
        "requests:\n"
        "  - tool: read_file\n    path: app.py\n    start: 1\n    end: 2\n"
        "  - tool: read_file\n    path: b.py\n    start: 1\n    end: 2\n"
        "  - tool: read_file\n    path: c.py\n    start: 1\n    end: 2\n",
        CONFIRM,
    )
    _, report = investigate(
        [finding()], changeset(), repo=repo, config=cfg(max_files=2), complete=complete
    )
    refused = [op for op in report.operations if op.outcome is InvestigationOutcome.REFUSED]
    assert len(refused) == 1
    assert "file limit" in refused[0].note


def test_a_search_is_refused_when_the_checkout_cannot_be_trusted(repo: Path):
    """There is no changeset-backed fallback for a search: it looks outside the diff."""
    file = ChangedFile(path="app.py", new_content="def charge(amount):\n    return amount * 2\n")
    change = ChangeSet(files=[file], origin="github", head_sha="b" * 40)
    access = availability.resolve(change, repo)
    assert not access.searches


# --- config and CLI ----------------------------------------------------------


def test_the_stage_is_on_by_default():
    assert Config().review.investigate.enabled is True


def test_the_env_var_switches_it_off(tmp_path: Path, monkeypatch):
    from roborak.core.config import load_config

    monkeypatch.setattr("roborak.core.config.USER_CONFIG_PATH", tmp_path / "absent.yaml")
    monkeypatch.setenv("ROBORAK_NO_INVESTIGATE", "1")
    assert load_config(tmp_path).review.investigate.enabled is False


def test_the_section_rejects_a_typo():
    with pytest.raises(ValidationError):
        Config.model_validate({"review": {"investigate": {"max_round": 2}}})


def test_the_reviewer_skips_the_stage_when_it_is_switched_off(repo: Path):
    from tests.test_pipeline import StubLLM

    config = Config()
    config.review.investigate.enabled = False
    llm = StubLLM(reply="findings: []")
    result = Reviewer(config=config, repo=repo, llm=llm).review(changeset())
    assert result.investigation is None


def test_a_review_without_a_model_never_investigates(repo: Path):
    result = Reviewer(config=Config(), repo=repo, llm=None).review(changeset())
    assert result.investigation is None


# --- rendering ---------------------------------------------------------------


def test_json_says_nothing_when_the_stage_never_ran():
    """An absent key means nobody investigated; it must never mean "all clear"."""
    payload = json.loads(json_out.render(ReviewResult(changeset=changeset())))
    assert "investigation" not in payload


def test_json_carries_the_record_when_it_did(repo: Path):
    complete = replies(
        "requests:\n  - tool: read_file\n    path: app.py\n    start: 5\n    end: 6\n",
        CONFIRM,
    )
    result = ReviewResult(changeset=changeset())
    findings, report = investigate(
        [finding()], changeset(), repo=repo, config=cfg(), complete=complete
    )
    result.findings, result.investigation = findings, report

    payload = json.loads(json_out.render(result))["investigation"]
    assert payload["status"] == "completed"
    assert payload["candidates"] == 1
    assert payload["decisions"][0]["disposition"] == "confirm"
    assert payload["operations"][0]["tool"] == "read_file"
    assert "caller" in payload["operations"][0]["result"]


def test_the_agent_shape_keeps_decisions_and_drops_the_file_contents(repo: Path):
    complete = replies(
        "requests:\n  - tool: read_file\n    path: app.py\n    start: 5\n    end: 6\n",
        CONFIRM,
    )
    result = ReviewResult(changeset=changeset())
    _, result.investigation = investigate(
        [finding()], changeset(), repo=repo, config=cfg(), complete=complete
    )

    payload = json.loads(json_out.render(result, agent=True))["investigation"]
    assert payload["decisions"][0]["disposition"] == "confirm"
    assert "result" not in payload["operations"][0]


def test_the_schema_version_moved():
    assert json_out.SCHEMA_VERSION == 5


def test_markdown_renders_the_section(repo: Path):
    result = ReviewResult(changeset=changeset())
    _, result.investigation = investigate(
        [finding()], changeset(), repo=repo, config=cfg(), complete=replies(CONFIRM)
    )
    body = markdown.render(result)
    assert "Investigation" in body
    assert "confirmed" in body


def test_markdown_is_silent_when_there_was_nothing_to_settle():
    result = ReviewResult(changeset=changeset())
    result.investigation = InvestigationReport(status=InvestigationStatus.SKIPPED)
    assert "Investigation" not in markdown.render(result)


def test_markdown_speaks_up_when_the_checkout_did_not_match():
    """The one case an absent section would be read as an all-clear."""
    result = ReviewResult(changeset=changeset())
    result.investigation = InvestigationReport(
        status=InvestigationStatus.UNAVAILABLE,
        candidates=1,
        notes=["the checkout is at abc but the change is at def."],
    )
    body = markdown.render(result)
    assert "Investigation" in body
    assert "no matching checkout" in body


def test_the_terminal_view_prints_a_line(repo: Path, capsys):
    from rich.console import Console

    result = ReviewResult(changeset=changeset())
    _, result.investigation = investigate(
        [finding()], changeset(), repo=repo, config=cfg(), complete=replies(CONFIRM)
    )
    terminal.render(result, Console(force_terminal=False, width=100), repo)
    assert "investigation:" in capsys.readouterr().out
