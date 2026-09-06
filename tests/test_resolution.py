"""Retiring roborak's own review threads once later commits have fixed them.

Three layers, in the order the feature runs them: reading the open threads back
off a forge, deciding whether the commits since actually fixed each one, and
replying-then-resolving on the forge. The git repositories here are real, because
the whole verification step is a question about commit history and a hand-written
fixture would prove nothing about it.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest

from roborak.analysis.resolution import verify_fixes
from roborak.core.config import InvestigateConfig
from roborak.core.models import ChangeSet, FixVerdict, ForgeRef
from roborak.publish.base import PublishReport, remote_state, resolve_fixed
from roborak.publish.github import GitHubPublisher
from roborak.publish.gitlab import GitLabPublisher
from roborak.publish.threads import OpenThread, open_threads
from roborak.render.markdown import RESOLUTION_MARKER_PREFIX, resolution_markdown
from roborak.sources.base import SourceError
from roborak.sources.forge import ForgeClient, Target
from tests.test_forge import client_with, make_result

FINGERPRINT = "0123456789abcdef"
FINDING_BODY = (
    f"**SQL injection.**\n\nuser_id is concatenated into SQL.\n\n<!-- roborak:v1:{FINGERPRINT} -->"
)
RESOLUTION_BODY = f"Fixed.\n\n<!-- {RESOLUTION_MARKER_PREFIX}:{FINGERPRINT} -->"

GITLAB_TARGET = Target("gitlab", "gitlab.com", "acme/web", 298)
# httpx hands the handler a decoded path, so the encoded project comes back split.
DISCUSSION = "/api/v4/projects/acme/web/merge_requests/298/discussions/d1"
DISCUSSION_NOTES = f"{DISCUSSION}/notes"
GITHUB_TARGET = Target("github", "github.com", "acme/web", 42)


# --- reading the open threads back -----------------------------------------


def gitlab_discussion(
    identifier: str = "d1",
    *,
    body: str = FINDING_BODY,
    author: str = "roborak-bot",
    resolved: bool = False,
    replies: list[dict] | None = None,
    position: dict | None = None,
) -> dict:
    root = {
        "id": 1,
        "body": body,
        "author": {"username": author},
        "resolvable": True,
        "resolved": resolved,
        "position": {"new_path": "app/auth.py", "new_line": 11, "head_sha": "old111"}
        if position is None
        else position,
    }
    return {"id": identifier, "notes": [root, *(replies or [])]}


def gitlab_client(discussions: list[dict], monkeypatch=None) -> ForgeClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/discussions"):
            page = int(request.url.params.get("page", 1))
            return httpx.Response(200, json=discussions if page == 1 else [])
        return httpx.Response(200, json={})

    return client_with(handler, GITLAB_TARGET)


def test_gitlab_reads_back_its_own_open_thread():
    threads = open_threads(gitlab_client([gitlab_discussion()]), GITLAB_TARGET, "roborak-bot")

    assert len(threads) == 1
    assert threads[0].key == "d1"
    assert threads[0].file == "app/auth.py"
    assert threads[0].line == 11
    assert threads[0].anchor_sha == "old111"
    assert threads[0].fingerprints == frozenset({FINGERPRINT})
    assert not threads[0].replied


@pytest.mark.parametrize(
    ("discussion", "why"),
    [
        (gitlab_discussion(resolved=True), "already resolved"),
        (gitlab_discussion(author="a-human"), "written by a person"),
        (gitlab_discussion(body="Looks good to me."), "carries no finding marker"),
        (
            gitlab_discussion(body="<!-- roborak:review -->\nThe report."),
            "is the summary comment, not an inline finding",
        ),
        (gitlab_discussion(position={}), "has no file to check the history of"),
    ],
)
def test_gitlab_leaves_protected_threads_alone(discussion, why):
    assert open_threads(gitlab_client([discussion]), GITLAB_TARGET, "roborak-bot") == [], why


def test_a_thread_that_already_carries_the_evidence_reply_is_marked_replied():
    """Idempotency: the marker on the forge is the only record that reply landed."""
    discussion = gitlab_discussion(
        replies=[{"id": 2, "body": RESOLUTION_BODY, "author": {"username": "roborak-bot"}}]
    )

    threads = open_threads(gitlab_client([discussion]), GITLAB_TARGET, "roborak-bot")

    assert threads[0].replied


def test_a_ci_token_that_cannot_name_itself_falls_back_to_the_bot_test():
    mine = gitlab_discussion("d1", author="roborak-bot")
    mine["notes"][0]["author"]["bot"] = True
    theirs = gitlab_discussion("d2", author="a-human")

    threads = open_threads(gitlab_client([mine, theirs]), GITLAB_TARGET, "")

    assert [thread.key for thread in threads] == ["d1"]


def github_thread(
    *,
    body: str = FINDING_BODY,
    author: str = "roborak-bot",
    resolved: bool = False,
    replies: list[dict] | None = None,
) -> dict:
    root = {
        "body": body,
        "author": {"login": author, "__typename": "User"},
        "originalCommit": {"oid": "old111"},
    }
    return {
        "id": "PRRT_kwabc",
        "isResolved": resolved,
        "path": "app/auth.py",
        "line": 11,
        "comments": {"nodes": [root, *(replies or [])]},
    }


def github_client(nodes: list[dict], *, errors: list[dict] | None = None) -> ForgeClient:
    def handler(request: httpx.Request) -> httpx.Response:
        payload: dict = {
            "data": {"repository": {"pullRequest": {"reviewThreads": {"nodes": nodes}}}}
        }
        if errors:
            payload["errors"] = errors
        return httpx.Response(200, json=payload)

    return client_with(handler, GITHUB_TARGET)


def test_github_reads_its_review_threads_over_graphql():
    threads = open_threads(github_client([github_thread()]), GITHUB_TARGET, "roborak-bot")

    assert len(threads) == 1
    assert threads[0].key == "PRRT_kwabc"
    assert threads[0].anchor_sha == "old111"
    assert threads[0].file == "app/auth.py"


@pytest.mark.parametrize(
    "thread",
    [
        github_thread(resolved=True),
        github_thread(author="a-human"),
        github_thread(body="Nice catch, thanks."),
    ],
)
def test_github_leaves_protected_threads_alone(thread):
    assert open_threads(github_client([thread]), GITHUB_TARGET, "roborak-bot") == []


def test_a_graphql_error_leaves_every_thread_alone_rather_than_reporting_none():
    """A 200 carrying `errors` is a failure, and must not read as an empty list."""
    client = github_client([github_thread()], errors=[{"message": "Resource not accessible"}])

    assert open_threads(client, GITHUB_TARGET, "roborak-bot") == []


def test_graphql_raises_rather_than_returning_a_rejected_answer():
    client = github_client([], errors=[{"message": "Bad credentials"}])

    with pytest.raises(SourceError, match="Bad credentials"):
        client.graphql("query {}", {})


def test_graphql_lives_beside_the_rest_api_on_an_enterprise_host():
    assert Target("github", "github.com", "a/b", 1).graphql_url == "https://api.github.com/graphql"
    assert Target("github", "gh.corp", "a/b", 1).graphql_url == "https://gh.corp/api/graphql"


def test_remote_state_carries_the_open_threads_alongside_the_fingerprints(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/user"):
            return httpx.Response(200, json={"username": "roborak-bot"})
        if request.url.path.endswith("/discussions"):
            page = int(request.url.params.get("page", 1))
            return httpx.Response(200, json=[gitlab_discussion()] if page == 1 else [])
        return httpx.Response(200, json=[])

    monkeypatch.setattr("roborak.publish.base.ForgeClient", lambda t, tok: client_with(handler, t))

    state = remote_state(GITLAB_TARGET, "tok")

    assert FINGERPRINT in state.fingerprints
    assert [thread.key for thread in state.open_threads] == ["d1"]


# --- verifying that later commits fixed it ---------------------------------


def git(repo: Path, *args: str) -> str:
    done = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True)
    return done.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "t@example.com")
    git(root, "config", "user.name", "Test")
    (root / "app").mkdir()
    (root / "app" / "auth.py").write_text(
        "def lookup(db, user_id):\n    return db.execute('SELECT * FROM s WHERE u = ' + user_id)\n"
    )
    git(root, "add", "-A")
    git(root, "commit", "-qm", "Add session lookup")
    return root


def fix_it(repo: Path, message: str = "Parameterise the session lookup", *, note: str = "") -> str:
    (repo / "app" / "auth.py").write_text(
        f"{note}def lookup(db, user_id):\n"
        "    return db.execute('SELECT * FROM s WHERE u = ?', (user_id,))\n"
    )
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", message)
    return git(repo, "rev-parse", "HEAD")


def changeset_at(repo: Path) -> ChangeSet:
    head = git(repo, "rev-parse", "HEAD")
    return ChangeSet(
        files=[],
        origin="gitlab",
        head_sha=head,
        forge_ref=ForgeRef(
            provider="gitlab", host="gitlab.com", project="acme/web", number=298, head_sha=head
        ),
    )


def thread_at(repo: Path, anchor: str, *, replied: bool = False) -> OpenThread:
    return OpenThread(
        key="d1",
        fingerprints=frozenset({FINGERPRINT}),
        body=FINDING_BODY,
        file="app/auth.py",
        line=11,
        anchor_sha=anchor,
        replied=replied,
    )


@dataclass
class ScriptedLLM:
    """Replays canned replies and records the prompts it was handed."""

    replies: list[str]
    calls: int = 0

    def __post_init__(self) -> None:
        self.prompts: list[tuple[str, str]] = []

    def __call__(self, system: str, user: str) -> str:
        self.prompts.append((system, user))
        self.calls += 1
        return self.replies[min(self.calls - 1, len(self.replies) - 1)]


def verdict_reply(commits: list[str], state: str = "fixed") -> str:
    shas = ", ".join(f'"{sha}"' for sha in commits)
    return (
        "verdicts:\n"
        "  - thread: t1\n"
        f"    state: {state}\n"
        f"    commits: [{shas}]\n"
        "    summary: The query is now parameterised.\n"
    )


def run_verify(threads, repo: Path, llm, config: InvestigateConfig | None = None):
    return verify_fixes(
        threads,
        changeset_at(repo),
        repo=repo,
        config=config or InvestigateConfig(),
        complete=llm,
    )


def test_a_fix_in_a_later_commit_is_verified_and_attributed(repo):
    anchor = git(repo, "rev-parse", "HEAD")
    fixing = fix_it(repo)
    llm = ScriptedLLM([verdict_reply([fixing[:12]])])

    [verdict] = run_verify([thread_at(repo, anchor)], repo, llm)

    assert verdict.state == "fixed"
    assert verdict.commits == [fixing]
    assert verdict.attributable
    # The published finding and the history since are both in front of the model.
    assert "SQL injection" in llm.prompts[0][1]
    assert "Parameterise the session lookup" in llm.prompts[0][1]


def test_several_commits_are_all_attributed(repo):
    anchor = git(repo, "rev-parse", "HEAD")
    first = fix_it(repo, "Start parameterising the lookup", note="# work in progress\n")
    second = fix_it(repo, "Finish parameterising the lookup")
    llm = ScriptedLLM([verdict_reply([second[:12], first[:12]])])

    [verdict] = run_verify([thread_at(repo, anchor)], repo, llm)

    assert verdict.commits == [second, first]


def test_a_thread_the_model_calls_not_fixed_stays_open(repo):
    anchor = git(repo, "rev-parse", "HEAD")
    fixing = fix_it(repo)
    llm = ScriptedLLM([verdict_reply([fixing[:12]], state="not_fixed")])

    [verdict] = run_verify([thread_at(repo, anchor)], repo, llm)

    assert verdict.state == "not_fixed"
    assert not verdict.attributable


@pytest.mark.parametrize(
    ("reply", "why"),
    [
        ("verdicts:\n  - thread: t9\n    state: fixed\n", "names an id roborak never issued"),
        ("not yaml at all: [", "could not be read"),
        ("verdicts: []\n", "settled nothing"),
        ("summary: I had a look around\n", "answered in the wrong shape"),
    ],
)
def test_an_unusable_reply_leaves_the_thread_inconclusive(repo, reply, why):
    anchor = git(repo, "rev-parse", "HEAD")
    fix_it(repo)

    [verdict] = run_verify([thread_at(repo, anchor)], repo, ScriptedLLM([reply]))

    assert verdict.state == "inconclusive", why
    assert not verdict.attributable


def test_a_fix_attributed_to_a_commit_roborak_never_showed_it_is_not_attributable(repo):
    """Attribution is the part a reader clicks, so an invented sha is dropped."""
    anchor = git(repo, "rev-parse", "HEAD")
    fix_it(repo)
    llm = ScriptedLLM([verdict_reply(["deadbeefcafe"])])

    [verdict] = run_verify([thread_at(repo, anchor)], repo, llm)

    assert verdict.state == "fixed"
    assert verdict.commits == []
    assert not verdict.attributable


def test_a_provider_failure_leaves_the_thread_inconclusive(repo):
    anchor = git(repo, "rev-parse", "HEAD")
    fix_it(repo)

    def explode(system: str, user: str) -> str:
        raise RuntimeError("the provider is down")

    [verdict] = run_verify([thread_at(repo, anchor)], repo, explode)

    assert verdict.state == "inconclusive"


def test_no_commit_touched_the_file_so_the_model_is_never_asked(repo):
    anchor = git(repo, "rev-parse", "HEAD")
    (repo / "README.md").write_text("unrelated\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "Add a readme")
    llm = ScriptedLLM([verdict_reply(["whatever"])])

    [verdict] = run_verify([thread_at(repo, anchor)], repo, llm)

    assert verdict.state == "inconclusive"
    assert llm.calls == 0


def test_an_anchor_this_checkout_does_not_have_is_inconclusive(repo):
    """Routine on the shallow clone CI hands out; it is not evidence of a fix."""
    fix_it(repo)
    llm = ScriptedLLM([verdict_reply(["whatever"])])

    [verdict] = run_verify([thread_at(repo, "0" * 40)], repo, llm)

    assert verdict.state == "inconclusive"
    assert llm.calls == 0


def test_a_dirty_checkout_leaves_every_thread_open(repo):
    anchor = git(repo, "rev-parse", "HEAD")
    fix_it(repo)
    (repo / "app" / "auth.py").write_text("# someone was editing this\n")
    llm = ScriptedLLM([verdict_reply(["whatever"])])

    [verdict] = run_verify([thread_at(repo, anchor)], repo, llm)

    assert verdict.state == "inconclusive"
    assert llm.calls == 0


def test_a_checkout_of_a_different_revision_leaves_every_thread_open(repo):
    anchor = git(repo, "rev-parse", "HEAD")
    fix_it(repo)
    changeset = changeset_at(repo)
    changeset.head_sha = "f" * 40
    changeset.forge_ref.head_sha = "f" * 40  # type: ignore[union-attr]
    llm = ScriptedLLM([verdict_reply(["whatever"])])

    verdicts = verify_fixes(
        [thread_at(repo, anchor)],
        changeset,
        repo=repo,
        config=InvestigateConfig(),
        complete=llm,
    )

    assert verdicts[0].state == "inconclusive"
    assert llm.calls == 0


def test_the_model_may_read_the_current_file_before_deciding(repo):
    anchor = git(repo, "rev-parse", "HEAD")
    fixing = fix_it(repo)
    llm = ScriptedLLM(
        [
            "requests:\n  - tool: read_file\n    path: app/auth.py\n    start: 1\n    end: 5\n",
            verdict_reply([fixing[:12]]),
        ]
    )

    [verdict] = run_verify([thread_at(repo, anchor)], repo, llm)

    assert verdict.state == "fixed"
    assert llm.calls == 2
    # What it read comes back in the next round's prompt.
    assert "SELECT * FROM s WHERE u = ?" in llm.prompts[1][1]


def test_the_state_stores_head_stands_in_for_a_thread_with_no_anchor(repo):
    anchor = git(repo, "rev-parse", "HEAD")
    fixing = fix_it(repo)
    llm = ScriptedLLM([verdict_reply([fixing[:12]])])

    [verdict] = verify_fixes(
        [thread_at(repo, "")],
        changeset_at(repo),
        repo=repo,
        config=InvestigateConfig(),
        complete=llm,
        fallback_base=anchor,
    )

    assert verdict.commits == [fixing]


# --- the reply, and only then the resolve ----------------------------------


def fixed_verdict(commits: list[str] | None = None) -> FixVerdict:
    return FixVerdict(
        thread="d1",
        state="fixed",
        commits=commits or ["abc1234def5678"],
        summary="The query is now parameterised.",
    )


def test_the_reply_names_the_commits_and_carries_the_idempotency_marker():
    body = resolution_markdown(
        fixed_verdict(["abc1234def5678"]),
        FINGERPRINT,
        commit_url=lambda sha: f"https://gitlab.com/acme/web/-/commit/{sha}",
    )

    assert "[`abc1234d`](https://gitlab.com/acme/web/-/commit/abc1234def5678)" in body
    assert "The query is now parameterised." in body
    assert f"<!-- {RESOLUTION_MARKER_PREFIX}:{FINGERPRINT} -->" in body


def test_a_commit_without_a_link_is_still_named():
    body = resolution_markdown(fixed_verdict(), FINGERPRINT, commit_url=lambda sha: None)

    assert "`abc1234d`" in body


def recorder(responses: dict[tuple[str, str], httpx.Response] | None = None):
    """A handler recording every write, answering GraphQL by mutation name."""
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.content) if request.content else {}
        query = str(body.get("query") or "")
        name = (
            "reply"
            if "addPullRequestReviewThreadReply" in query
            else "resolve"
            if "resolveReviewThread" in query
            else path
        )
        calls.append((request.method, name))
        if (override := (responses or {}).get((request.method, name))) is not None:
            return override
        return httpx.Response(200, json={"data": {"ok": True}, "id": 9})

    handler.calls = calls  # type: ignore[attr-defined]
    return handler


def gitlab_writes(calls: list[tuple[str, str]]) -> list[tuple[str, str]]:
    return [(method, path) for method, path in calls if "/discussions/d1" in path]


def test_gitlab_replies_before_it_resolves():
    handler = recorder()
    client = client_with(handler, GITLAB_TARGET)
    report = PublishReport()

    resolve_fixed(
        client,
        GITLAB_TARGET,
        [(thread_at(Path("."), "old111"), fixed_verdict())],
        report,
        make_result(),
    )

    assert gitlab_writes(handler.calls) == [
        ("POST", DISCUSSION_NOTES),
        ("PUT", DISCUSSION),
    ]
    assert report.resolved == ["app/auth.py:11"]
    assert report.resolution_failed == []


def test_github_replies_before_it_resolves():
    handler = recorder()
    client = client_with(handler, GITHUB_TARGET)
    report = PublishReport()

    resolve_fixed(
        client,
        GITHUB_TARGET,
        [(thread_at(Path("."), "old111"), fixed_verdict())],
        report,
        make_result(),
    )

    assert handler.calls == [("POST", "reply"), ("POST", "resolve")]
    assert report.resolved == ["app/auth.py:11"]


def test_a_failed_reply_never_resolves_the_thread():
    handler = recorder({("POST", DISCUSSION_NOTES): httpx.Response(403, text="nope")})
    report = PublishReport()

    resolve_fixed(
        client_with(handler, GITLAB_TARGET),
        GITLAB_TARGET,
        [(thread_at(Path("."), "old111"), fixed_verdict())],
        report,
        make_result(),
    )

    assert [method for method, _ in gitlab_writes(handler.calls)] == ["POST"]
    assert report.resolved == []
    assert report.resolution_failed[0][0] == "app/auth.py:11"


def test_a_failed_resolve_is_reported_rather_than_recorded_as_resolved():
    handler = recorder({("PUT", DISCUSSION): httpx.Response(403, text="nope")})
    report = PublishReport()

    resolve_fixed(
        client_with(handler, GITLAB_TARGET),
        GITLAB_TARGET,
        [(thread_at(Path("."), "old111"), fixed_verdict())],
        report,
        make_result(),
    )

    assert report.resolved == []
    assert len(report.resolution_failed) == 1


def test_a_thread_already_replied_to_only_has_its_resolve_retried():
    """The second half of idempotency: the reply landed, the resolve did not."""
    handler = recorder()
    report = PublishReport()

    resolve_fixed(
        client_with(handler, GITLAB_TARGET),
        GITLAB_TARGET,
        [(thread_at(Path("."), "old111", replied=True), fixed_verdict())],
        report,
        make_result(),
    )

    assert [method for method, _ in gitlab_writes(handler.calls)] == ["PUT"]
    assert report.resolved == ["app/auth.py:11"]


def test_an_unattributable_verdict_touches_nothing():
    handler = recorder()
    report = PublishReport()

    resolve_fixed(
        client_with(handler, GITLAB_TARGET),
        GITLAB_TARGET,
        [
            (thread_at(Path("."), "old111"), FixVerdict(thread="d1", state="inconclusive")),
            (thread_at(Path("."), "old111"), FixVerdict(thread="d1", state="fixed", commits=[])),
        ],
        report,
        make_result(),
    )

    assert gitlab_writes(handler.calls) == []
    assert report.resolved == []
    assert report.resolution_failed == []


# --- through the publishers ------------------------------------------------


def test_the_gitlab_publisher_resolves_alongside_the_review(monkeypatch):
    handler = recorder()
    monkeypatch.setattr(
        "roborak.publish.gitlab.ForgeClient", lambda t, tok: client_with(handler, t)
    )

    report = GitLabPublisher(
        target=GITLAB_TARGET,
        token="tok",
        post_check=False,
        resolutions=((thread_at(Path("."), "old111"), fixed_verdict()),),
    ).publish(make_result())

    assert report.resolved == ["app/auth.py:11"]
    assert ("PUT", DISCUSSION) in handler.calls


def test_the_github_publisher_resolves_alongside_the_review(monkeypatch):
    handler = recorder()
    monkeypatch.setattr(
        "roborak.publish.github.ForgeClient", lambda t, tok: client_with(handler, t)
    )
    result = make_result()
    result.changeset.forge_ref.provider = "github"  # type: ignore[union-attr]

    report = GitHubPublisher(
        target=GITHUB_TARGET,
        token="tok",
        post_check=False,
        resolutions=((thread_at(Path("."), "old111"), fixed_verdict()),),
    ).publish(result)

    assert report.resolved == ["app/auth.py:11"]
    assert ("POST", "reply") in handler.calls
    assert ("POST", "resolve") in handler.calls


# --- the CLI step that pairs verdicts back to threads ----------------------


def test_the_cli_pairs_verified_verdicts_back_to_their_threads(repo, monkeypatch):
    """Only an attributable fix comes back; everything else leaves its thread open."""
    from rich.console import Console

    from roborak.analysis.reviewer import Reviewer
    from roborak.cli import shared
    from roborak.cli.commands.review import _resolutions
    from roborak.core.config import Config
    from roborak.core.models import ReviewResult
    from roborak.publish.base import RemoteState
    from roborak.state.store import StateStore, review_key

    anchor = git(repo, "rev-parse", "HEAD")
    StateStore(repo).record(review_key("gitlab", "gitlab.com", "acme/web", 298), [], anchor)

    fixed = thread_at(repo, "old111")
    unfixed = OpenThread(
        key="d2",
        fingerprints=frozenset({"f" * 16}),
        body=FINDING_BODY,
        file="app/other.py",
        line=3,
        anchor_sha="old111",
        replied=False,
    )

    seen: dict[str, object] = {}

    def fake_verify(self, threads, changeset, *, fallback_base=""):
        seen["threads"] = [thread.key for thread in threads]
        seen["fallback_base"] = fallback_base
        return [
            FixVerdict(thread="d1", state="fixed", commits=["abc1234"], summary="Parameterised."),
            FixVerdict(thread="d2", state="inconclusive"),
        ]

    monkeypatch.setattr(Reviewer, "verify_fixes", fake_verify)

    config = Config()
    session = shared.Session(
        console=Console(),
        repo=repo,
        config=config,
        changeset=changeset_at(repo),
        llm=object(),  # type: ignore[arg-type]
        target=GITLAB_TARGET,
        token="tok",
    )
    reviewer = Reviewer(config=config, repo=repo)

    resolutions = _resolutions(
        Console(),
        session,
        reviewer,
        ReviewResult(),
        RemoteState(open_threads=(fixed, unfixed)),
    )

    assert seen["threads"] == ["d1", "d2"]
    assert seen["fallback_base"] == anchor
    assert [thread.key for thread, _ in resolutions] == ["d1"]


def test_the_cli_asks_nothing_when_there_are_no_open_threads(repo):
    from rich.console import Console

    from roborak.analysis.reviewer import Reviewer
    from roborak.cli import shared
    from roborak.cli.commands.review import _resolutions
    from roborak.core.config import Config
    from roborak.core.models import ReviewResult
    from roborak.publish.base import RemoteState

    config = Config()
    session = shared.Session(
        console=Console(),
        repo=repo,
        config=config,
        changeset=changeset_at(repo),
        llm=None,
        target=GITLAB_TARGET,
        token="tok",
    )

    assert (
        _resolutions(
            Console(), session, Reviewer(config=config, repo=repo), ReviewResult(), RemoteState()
        )
        == ()
    )


# --- the read-only boundary this pass hands the model ----------------------


def test_the_model_may_search_the_tree_but_nothing_else(repo):
    anchor = git(repo, "rev-parse", "HEAD")
    fixing = fix_it(repo)
    llm = ScriptedLLM(
        [
            "requests:\n"
            "  - tool: search\n"
            "    pattern: execute\n"
            "    path: app\n"
            "  - tool: show_diff\n"
            "    path: app/auth.py\n",
            verdict_reply([fixing[:12]]),
        ]
    )

    [verdict] = run_verify([thread_at(repo, anchor)], repo, llm)

    assert verdict.state == "fixed"
    # The search ran; the operation this pass does not offer never reached the tree.
    assert "app/auth.py:2:" in llm.prompts[1][1]
    assert "show_diff" not in llm.prompts[1][1]
