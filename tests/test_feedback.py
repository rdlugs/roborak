"""Reviewer dismissals that keep a repeated finding out of later reviews.

Four layers, in the order the feature runs them: reading marker replies back off
a forge, remembering them in local state, holding matching findings back, and
saying so on every surface the review reaches.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from roborak.analysis.feedback import apply_feedback, entries_from
from roborak.cli.main import app
from roborak.core.config import FeedbackConfig
from roborak.core.models import Finding, ReviewResult
from roborak.core.severity import Category, FeedbackVerdict, Severity
from roborak.publish.base import RemoteState
from roborak.publish.threads import Dismissal, sweep_threads, verdict_in
from roborak.render import markdown
from roborak.render.json_out import to_dict
from roborak.state.store import FeedbackEntry, StateStore
from tests.test_cli import (  # noqa: F401 - fixtures are picked up by name
    EXIT_OK,
    MR_URL,
    _mr_session,
    repo,
    runner,
    stub_review_progress,
)
from tests.test_resolution import (
    FINDING_BODY,
    FINGERPRINT,
    GITHUB_TARGET,
    GITLAB_TARGET,
    github_client,
    github_thread,
    gitlab_client,
    gitlab_discussion,
)

MARKERS = FeedbackConfig().markers


def finding(**overrides: object) -> Finding:
    fields: dict[str, object] = {
        "file": "app/auth.py",
        "start_line": 11,
        "end_line": 11,
        "severity": Severity.CRITICAL,
        "category": Category.SECURITY,
        "title": "SQL injection",
        "body": "user_id is concatenated into SQL.",
    }
    fields.update(overrides)
    return Finding.model_validate(fields)


def entry(verdict: FeedbackVerdict = FeedbackVerdict.FALSE_POSITIVE, **overrides: str):
    fields = {"author": "alice", "recorded_at": "2026-01-01T00:00:00Z"} | overrides
    return FeedbackEntry(verdict=verdict, **fields)


def gitlab_reply(body: str, *, author: str = "alice", bot: bool = False, at: str = "2026-01-02"):
    return {"body": body, "author": {"username": author, "bot": bot}, "created_at": at}


def github_reply(body: str, *, author: str = "alice", kind: str = "User", at: str = "2026-01-02"):
    return {"body": body, "author": {"login": author, "__typename": kind}, "createdAt": at}


# --- reading marker replies back -------------------------------------------


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("roborak: false-positive", FeedbackVerdict.FALSE_POSITIVE),
        ("Checked this.\n  ROBORAK: Ignore - handled upstream", FeedbackVerdict.IGNORED),
        ("roborak: accept", FeedbackVerdict.ACCEPTED),
        ("> roborak: ignore", None),
        ("I think roborak: ignore applies", None),
        ("roborak: ignored-by-me", None),
        ("roborak: ignorethis", None),
        ("roborak: maybe", None),
    ],
)
def test_a_marker_counts_only_at_the_start_of_a_line(body, expected):
    assert verdict_in(body, MARKERS) is expected


@pytest.mark.parametrize("resolved", [False, True])
def test_gitlab_collects_a_dismissal_from_open_and_resolved_threads(resolved):
    discussion = gitlab_discussion(
        resolved=resolved, replies=[gitlab_reply("roborak: false-positive")]
    )

    found = sweep_threads(gitlab_client([discussion]), GITLAB_TARGET, "roborak-bot", MARKERS)

    assert found.dismissals == [
        Dismissal(
            fingerprints=frozenset({FINGERPRINT}),
            verdict=FeedbackVerdict.FALSE_POSITIVE,
            author="alice",
            file="app/auth.py",
            title="SQL injection",
            recorded_at="2026-01-02",
        )
    ]
    assert [t.key for t in found.open] == ([] if resolved else ["d1"])


@pytest.mark.parametrize(
    "discussion",
    [
        gitlab_discussion(replies=[gitlab_reply("roborak: ignore", author="ci", bot=True)]),
        gitlab_discussion(replies=[gitlab_reply("roborak: ignore", author="roborak-bot")]),
        gitlab_discussion(author="a-human", replies=[gitlab_reply("roborak: ignore")]),
        gitlab_discussion(body="Nice.", replies=[gitlab_reply("roborak: ignore")]),
        gitlab_discussion(replies=[gitlab_reply("thanks, will fix")]),
    ],
    ids=["bot-reply", "own-reply", "not-roborak-thread", "no-fingerprint", "no-marker"],
)
def test_gitlab_ignores_replies_that_are_not_a_person_dismissing_roborak(discussion):
    found = sweep_threads(gitlab_client([discussion]), GITLAB_TARGET, "roborak-bot", MARKERS)

    assert found.dismissals == []


def test_the_root_comment_itself_is_never_read_as_a_dismissal():
    discussion = gitlab_discussion(body=f"roborak: ignore\n\n{FINDING_BODY}")

    found = sweep_threads(gitlab_client([discussion]), GITLAB_TARGET, "", MARKERS)

    assert found.dismissals == []


def test_the_latest_marker_reply_wins():
    discussion = gitlab_discussion(
        replies=[
            gitlab_reply("roborak: ignore", author="bob", at="2026-01-03"),
            gitlab_reply("roborak: accept", author="alice", at="2026-01-02"),
        ]
    )

    found = sweep_threads(gitlab_client([discussion]), GITLAB_TARGET, "roborak-bot", MARKERS)

    assert [(d.verdict, d.author) for d in found.dismissals] == [(FeedbackVerdict.IGNORED, "bob")]


def test_no_markers_means_no_dismissals():
    discussion = gitlab_discussion(replies=[gitlab_reply("roborak: ignore")])

    found = sweep_threads(gitlab_client([discussion]), GITLAB_TARGET, "roborak-bot", {})

    assert found.dismissals == []
    assert [t.key for t in found.open] == ["d1"]


@pytest.mark.parametrize("resolved", [False, True])
def test_github_collects_a_dismissal_over_graphql(resolved):
    thread = github_thread(resolved=resolved, replies=[github_reply("roborak: ignore")])

    found = sweep_threads(github_client([thread]), GITHUB_TARGET, "roborak-bot", MARKERS)

    assert [(d.verdict, d.author, d.recorded_at) for d in found.dismissals] == [
        (FeedbackVerdict.IGNORED, "alice", "2026-01-02")
    ]
    assert len(found.open) == (0 if resolved else 1)


def test_github_ignores_a_bot_reply():
    thread = github_thread(replies=[github_reply("roborak: ignore", author="dep", kind="Bot")])

    found = sweep_threads(github_client([thread]), GITHUB_TARGET, "roborak-bot", MARKERS)

    assert found.dismissals == []


# --- remembering them ------------------------------------------------------


def test_feedback_round_trips_through_state(tmp_path: Path):
    store = StateStore(tmp_path)
    store.record_feedback([("aaaa", entry()), ("bbbb", entry(FeedbackVerdict.ACCEPTED))], 10)

    remembered = store.feedback()

    assert remembered["aaaa"].verdict is FeedbackVerdict.FALSE_POSITIVE
    assert remembered["bbbb"].verdict is FeedbackVerdict.ACCEPTED


def test_feedback_keeps_the_newest_entries_within_the_limit(tmp_path: Path):
    store = StateStore(tmp_path)
    store.record_feedback(
        [(f"fp{day}", entry(recorded_at=f"2026-01-0{day}")) for day in range(1, 6)], 3
    )

    assert sorted(store.feedback()) == ["fp3", "fp4", "fp5"]


def test_an_older_dismissal_does_not_overwrite_a_newer_one(tmp_path: Path):
    store = StateStore(tmp_path)
    store.record_feedback(
        [
            ("fp", entry(FeedbackVerdict.ACCEPTED, recorded_at="2026-02")),
        ],
        5,
    )
    store.record_feedback([("fp", entry(FeedbackVerdict.IGNORED, recorded_at="2026-01"))], 5)

    assert store.feedback()["fp"].verdict is FeedbackVerdict.ACCEPTED


def test_a_malformed_feedback_entry_is_skipped(tmp_path: Path):
    store = StateStore(tmp_path)
    store.record_feedback([("good", entry())], 5)
    data = json.loads(store.path.read_text())
    data["feedback"]["bad"] = {"verdict": "who knows"}
    store.path.write_text(json.dumps(data))

    assert list(store.feedback()) == ["good"]


def test_clearing_review_state_keeps_reviewer_feedback(tmp_path: Path):
    store = StateStore(tmp_path)
    store.record_feedback([("fp", entry())], 5)
    store.record("gitlab:gitlab.com:acme/web#1", [finding()], "head")

    store.clear()

    assert "fp" in store.feedback()


def test_every_fingerprint_a_dismissal_names_becomes_an_entry():
    dismissal = Dismissal(
        frozenset({"aaaa", "bbbb"}), FeedbackVerdict.IGNORED, "alice", "a.py", "T", "2026"
    )

    assert [fp for fp, _ in entries_from([dismissal])] == ["aaaa", "bbbb"]


# --- holding findings back -------------------------------------------------


@pytest.mark.parametrize("which", ["fingerprint", "fingerprint_v2"])
def test_a_dismissed_model_finding_is_suppressed_and_listed(which):
    dismissed = finding()
    kept = finding(title="Unbounded loop", body="Never terminates.", start_line=12)
    result = ReviewResult(findings=[dismissed, kept])

    apply_feedback(result, {getattr(dismissed, which): entry()}, FeedbackConfig())

    assert result.findings == [kept]
    assert result.feedback is not None
    assert [(s.location, s.verdict, s.author) for s in result.feedback.suppressed] == [
        ("app/auth.py:11", FeedbackVerdict.FALSE_POSITIVE, "alice")
    ]


def test_a_static_finding_is_kept_and_counted_by_default():
    static = finding(source="static", tool="semgrep", rule_id="sql")
    result = ReviewResult(findings=[static])

    apply_feedback(result, {static.fingerprint_v2: entry()}, FeedbackConfig())

    assert result.findings == [static]
    assert result.feedback is not None
    assert result.feedback.suppressed == []
    assert result.feedback.static_kept == 1


def test_a_static_finding_is_suppressed_only_when_configured():
    static = finding(source="static", tool="semgrep", rule_id="sql")
    result = ReviewResult(findings=[static])

    apply_feedback(result, {static.fingerprint_v2: entry()}, FeedbackConfig(suppress_static=True))

    assert result.findings == []
    assert result.feedback is not None
    assert [s.source for s in result.feedback.suppressed] == ["static"]


def test_disabled_feedback_changes_nothing():
    dismissed = finding()
    result = ReviewResult(findings=[dismissed])

    apply_feedback(result, {dismissed.fingerprint_v2: entry()}, FeedbackConfig(enabled=False))

    assert result.findings == [dismissed]
    assert result.feedback is None


def test_no_match_leaves_no_report():
    result = ReviewResult(findings=[finding()])

    apply_feedback(result, {"ffffffffffffffff": entry()}, FeedbackConfig())

    assert len(result.findings) == 1
    assert result.feedback is None


def test_an_empty_marker_is_rejected():
    with pytest.raises(ValueError, match="must not be empty"):
        FeedbackConfig(markers={"  ": FeedbackVerdict.IGNORED})


# --- saying so --------------------------------------------------------------


def suppressed_result() -> ReviewResult:
    dismissed = finding()
    static = finding(source="static", tool="semgrep", rule_id="sql", title="Raw SQL")
    result = ReviewResult(findings=[dismissed, static])
    apply_feedback(
        result,
        {dismissed.fingerprint_v2: entry(), static.fingerprint_v2: entry()},
        FeedbackConfig(),
    )
    return result


@pytest.mark.parametrize("form", [markdown.Form.PUBLISHED, markdown.Form.TERMINAL])
def test_markdown_lists_what_feedback_suppressed(form):
    text = markdown.render(suppressed_result(), form=form)

    assert "1 finding(s) suppressed by prior reviewer feedback" in text
    assert "1 static finding(s) kept despite it" in text
    assert "`app/auth.py:11` SQL injection | critical | false positive | @alice |" in text


def test_json_carries_the_feedback_report():
    payload = to_dict(suppressed_result())

    assert payload["feedback"]["static_kept"] == 1
    assert payload["feedback"]["suppressed"][0]["verdict"] == "false_positive"


def test_terminal_panels_name_the_suppression(tmp_path: Path):
    from rich.console import Console

    from roborak.render import terminal

    console = Console(record=True, width=200)
    terminal.render(suppressed_result(), console, tmp_path)

    text = console.export_text()
    assert "feedback: 1 finding(s) suppressed by prior reviewer feedback" in text
    assert "false positive by @alice: app/auth.py:11 SQL injection" in text


def test_a_review_without_feedback_renders_no_section():
    assert "Reviewer feedback" not in markdown.render(ReviewResult(findings=[finding()]))


# --- end to end -------------------------------------------------------------


def test_a_post_run_remembers_dismissals_and_suppresses_the_repeat(repo: Path, monkeypatch):  # noqa: F811
    _mr_session(monkeypatch)
    dismissed = finding()
    remote = RemoteState(
        dismissals=(
            Dismissal(
                frozenset({dismissed.fingerprint_v2}),
                FeedbackVerdict.FALSE_POSITIVE,
                "alice",
                "app/auth.py",
                "SQL injection",
                "2026-01-02",
            ),
        )
    )
    seen: dict[str, object] = {}

    def fake_remote_state(target, token, markers=None):
        seen["markers"] = markers
        return remote

    monkeypatch.setattr("roborak.cli.commands.review.remote_state", fake_remote_state)

    outcome = runner.invoke(
        app,
        ["review", "--no-llm", "--mr", MR_URL, "--post", "--fail-on", "critical", "-C", str(repo)],
        input="n\n",
    )

    assert outcome.exit_code == EXIT_OK, outcome.output
    assert "suppressed by prior reviewer feedback" in outcome.output
    assert seen["markers"] == MARKERS
    assert StateStore(repo).feedback()[dismissed.fingerprint_v2].author == "alice"


def test_a_local_run_honours_remembered_feedback(repo: Path, monkeypatch):  # noqa: F811
    from roborak.analysis.reviewer import Reviewer

    dismissed = finding(file="app.py", start_line=1, end_line=1)
    monkeypatch.setattr(
        Reviewer, "review", lambda self, cs: ReviewResult(changeset=cs, findings=[dismissed])
    )
    StateStore(repo).record_feedback([(dismissed.fingerprint, entry())], 10)
    (repo / "app.py").write_text("def f():\n    return 2\n")

    outcome = runner.invoke(
        app,
        ["review", "--no-llm", "--uncommitted", "--fail-on", "major", "-C", str(repo)],
    )

    assert outcome.exit_code == EXIT_OK, outcome.output
    assert "suppressed by prior reviewer feedback" in outcome.output


def test_a_local_run_with_feedback_disabled_reports_everything(repo: Path, monkeypatch):  # noqa: F811
    from roborak.analysis.reviewer import Reviewer

    dismissed = finding(file="app.py", start_line=1, end_line=1)
    monkeypatch.setattr(
        Reviewer, "review", lambda self, cs: ReviewResult(changeset=cs, findings=[dismissed])
    )
    StateStore(repo).record_feedback([(dismissed.fingerprint, entry())], 10)
    (repo / ".roborak.yaml").write_text("review:\n  feedback:\n    enabled: false\n")
    (repo / "app.py").write_text("def f():\n    return 2\n")

    outcome = runner.invoke(
        app,
        ["review", "--no-llm", "--uncommitted", "--fail-on", "major", "-C", str(repo)],
    )

    assert outcome.exit_code != EXIT_OK
    assert "suppressed by prior reviewer feedback" not in outcome.output


def test_a_failed_state_write_still_credits_the_dismissal_just_read(tmp_path: Path, monkeypatch):
    from roborak.cli.commands.review import _apply_feedback
    from roborak.core.config import Config
    from roborak.state.store import StateWriteError

    dismissed = finding()
    store = StateStore(tmp_path)
    store.record_feedback(
        [(dismissed.fingerprint_v2, entry(FeedbackVerdict.ACCEPTED, author="old"))], 10
    )

    def refuse(self, entries, limit):
        raise StateWriteError("read-only")

    monkeypatch.setattr(StateStore, "record_feedback", refuse)
    remote = RemoteState(
        dismissals=(
            Dismissal(
                frozenset({dismissed.fingerprint_v2}),
                FeedbackVerdict.FALSE_POSITIVE,
                "alice",
                "app/auth.py",
                "SQL injection",
                "2026-03-01T00:00:00Z",
            ),
        )
    )
    result = ReviewResult(findings=[dismissed])

    _apply_feedback(tmp_path, Config(), result, remote)

    assert result.feedback is not None
    assert [(s.verdict, s.author) for s in result.feedback.suppressed] == [
        (FeedbackVerdict.FALSE_POSITIVE, "alice")
    ]


def test_a_lowered_limit_is_enforced_even_when_nothing_new_arrives(tmp_path: Path):
    store = StateStore(tmp_path)
    store.record_feedback(
        [(f"fp{day}", entry(recorded_at=f"2026-01-0{day}")) for day in range(1, 6)], 10
    )

    store.record_feedback([], 2)

    assert sorted(store.feedback()) == ["fp4", "fp5"]


def test_nothing_new_within_the_limit_writes_no_state(tmp_path: Path):
    StateStore(tmp_path).record_feedback([], 10)

    assert not (tmp_path / ".roborak").exists()


def test_terminal_panels_list_every_suppressed_finding(tmp_path: Path):
    from rich.console import Console

    from roborak.render import terminal

    findings = [finding(title=f"Problem {n}", body=f"Body {n}.") for n in range(7)]
    result = ReviewResult(findings=findings)
    apply_feedback(result, {f.fingerprint_v2: entry() for f in findings}, FeedbackConfig())

    console = Console(record=True, width=200)
    terminal.render(result, console, tmp_path)

    text = console.export_text()
    assert all(f"Problem {n}" in text for n in range(7))
