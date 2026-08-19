"""Forge sources and publishers.

The assertions that matter are on the *position payloads*: getting these wrong
puts review comments on unrelated lines of somebody's merge request, which is the
single worst thing this tool can do.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from roborak.core.models import ChangeSet, Finding, ForgeRef, ReviewResult
from roborak.core.severity import Category, Kind, Severity
from roborak.publish.base import finding_markdown, summary_markdown
from roborak.publish.github import GitHubPublisher
from roborak.publish.gitlab import GitLabPublisher
from roborak.sources.base import SourceError
from roborak.sources.forge import (
    ForgeClient,
    Target,
    parse_target,
    project_from_remote,
)
from roborak.sources.github import GitHubSource
from roborak.sources.gitlab import GitLabSource
from roborak.state.store import StateStore, review_key

PATCH = (
    "@@ -8,6 +8,9 @@ def get_session(request):\n"
    "     user_id = request.args.get('user_id')\n"
    "     if not user_id:\n"
    "         return None\n"
    "+    row = db.execute('SELECT * FROM s WHERE u = ' + user_id)\n"
    "+    if row.token == request.args.get('token'):\n"
    "+        return row\n"
    "     return None\n"
)


# -- target parsing --------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "provider", "expected"),
    [
        (
            "https://gitlab.com/acme/web/-/merge_requests/298",
            "gitlab",
            ("gitlab.com", "acme/web", 298),
        ),
        (
            "https://gitlab.example.com/group/sub/app/-/merge_requests/7",
            "gitlab",
            ("gitlab.example.com", "group/sub/app", 7),
        ),
        ("https://github.com/acme/web/pull/42", "github", ("github.com", "acme/web", 42)),
    ],
)
def test_url_parsing(url, provider, expected):
    target = parse_target(url, provider)
    assert (target.host, target.project, target.number) == expected


def test_bare_number_uses_the_supplied_project():
    target = parse_target("12", "gitlab", host="gitlab.example.com", project="a/b")
    assert (target.host, target.project, target.number) == ("gitlab.example.com", "a/b", 12)


def test_unparseable_reference_is_an_error():
    with pytest.raises(SourceError):
        parse_target("not-a-number-or-url", "gitlab")
    with pytest.raises(SourceError):
        parse_target("https://gitlab.com/acme/web/-/issues/3", "gitlab")


@pytest.mark.parametrize(
    ("remote", "expected"),
    [
        ("git@gitlab.com:acme/web.git", "acme/web"),
        ("git@gitlab.com:group/sub/web.git", "group/sub/web"),
        ("https://github.com/acme/web.git", "acme/web"),
        ("https://gitlab.com/acme/web", "acme/web"),
        ("", None),
    ],
)
def test_project_from_remote(remote, expected):
    assert project_from_remote(remote) == expected


def test_gitlab_encodes_the_project_path():
    assert Target("gitlab", "gitlab.com", "group/sub/app", 1).encoded_project == "group%2Fsub%2Fapp"


def test_api_base_handles_enterprise_hosts():
    assert Target("github", "github.com", "a/b", 1).api_base == "https://api.github.com"
    assert Target("github", "gh.corp", "a/b", 1).api_base == "https://gh.corp/api/v3"
    assert Target("gitlab", "gl.corp", "a/b", 1).api_base == "https://gl.corp/api/v4"


# -- HTTP client -----------------------------------------------------------


def client_with(handler, target: Target) -> ForgeClient:
    client = ForgeClient(target, "tok")
    client._client = httpx.Client(base_url=target.api_base, transport=httpx.MockTransport(handler))
    return client


@pytest.mark.parametrize(
    ("status", "fragment"),
    [
        (401, "rejected the token"),
        (403, "Not permitted"),
        (404, "Not found"),
        (500, "returned 500"),
    ],
)
def test_http_errors_are_translated(status, fragment):
    target = Target("gitlab", "gitlab.com", "a/b", 1)
    client = client_with(lambda request: httpx.Response(status, text="nope"), target)
    with pytest.raises(SourceError, match=fragment):
        client.get("/anything")


def test_pagination_stops_on_a_short_page():
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("page", 1))
        calls.append(page)
        return httpx.Response(200, json=[{"n": i} for i in range(100 if page == 1 else 3)])

    client = client_with(handler, Target("github", "github.com", "a/b", 1))
    assert len(client.paginate("/things")) == 103
    assert calls == [1, 2]


# -- GitLab source ---------------------------------------------------------

GITLAB_MR = {
    "title": "Add session lookup",
    "description": "Adds a session cache.",
    "target_branch": "main",
    "source_branch": "feature",
    "web_url": "https://gitlab.com/acme/web/-/merge_requests/298",
    "diff_refs": {"base_sha": "base111", "start_sha": "start222", "head_sha": "head333"},
}
GITLAB_CHANGES = {
    "diff_refs": GITLAB_MR["diff_refs"],
    "changes": [
        {"old_path": "app/auth.py", "new_path": "app/auth.py", "diff": PATCH},
        {
            "old_path": "old/name.py",
            "new_path": "new/name.py",
            "renamed_file": True,
            "diff": "@@ -1,1 +1,2 @@\n a\n+b\n",
        },
        {
            "old_path": "gone.py",
            "new_path": "gone.py",
            "deleted_file": True,
            "diff": "@@ -1,1 +0,0 @@\n-a\n",
        },
    ],
}


def test_gitlab_source(monkeypatch):
    target = Target("gitlab", "gitlab.com", "acme/web", 298)

    def handler(request: httpx.Request) -> httpx.Response:
        payload = GITLAB_CHANGES if request.url.path.endswith("/changes") else GITLAB_MR
        return httpx.Response(200, json=payload)

    monkeypatch.setattr(
        "roborak.sources.gitlab.ForgeClient", lambda t, tok: client_with(handler, t)
    )
    changeset = GitLabSource(target=target, token="tok").load()

    assert changeset.origin == "gitlab"
    assert changeset.title == "Add session lookup"
    assert changeset.base_ref == "main"
    assert changeset.forge_ref is not None
    assert changeset.forge_ref.start_sha == "start222"

    auth = changeset.file_by_path("app/auth.py")
    assert auth is not None
    # The patch adds three lines starting at new-file line 11.
    assert auth.added_lines == {11, 12, 13}
    assert auth.diff_position(11) == 5

    renamed = changeset.file_by_path("new/name.py")
    assert renamed is not None and renamed.change_type == "renamed"
    assert renamed.previous_path == "old/name.py"

    deleted = changeset.file_by_path("gone.py")
    assert deleted is not None and deleted.change_type == "deleted"


# -- GitHub source ---------------------------------------------------------

GITHUB_PR = {
    "title": "Add session lookup",
    "body": "Adds a session cache.",
    "html_url": "https://github.com/acme/web/pull/42",
    "base": {"sha": "base111", "ref": "main"},
    "head": {"sha": "head333", "ref": "feature"},
}
GITHUB_FILES = [
    {"filename": "app/auth.py", "status": "modified", "patch": PATCH},
    {"filename": "logo.png", "status": "modified"},  # binary: no patch
    {
        "filename": "new/name.py",
        "previous_filename": "old/name.py",
        "status": "renamed",
        "patch": "@@ -1,1 +1,2 @@\n a\n+b\n",
    },
]


def test_github_source(monkeypatch):
    target = Target("github", "github.com", "acme/web", 42)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/files"):
            return httpx.Response(200, json=GITHUB_FILES)
        return httpx.Response(200, json=GITHUB_PR)

    monkeypatch.setattr(
        "roborak.sources.github.ForgeClient", lambda t, tok: client_with(handler, t)
    )
    changeset = GitHubSource(target=target, token="tok").load()

    assert changeset.origin == "github"
    assert changeset.head_sha == "head333"
    auth = changeset.file_by_path("app/auth.py")
    assert auth is not None
    assert auth.added_lines == {11, 12, 13}

    binary = changeset.file_by_path("logo.png")
    assert binary is not None and binary.is_binary

    renamed = changeset.file_by_path("new/name.py")
    assert renamed is not None and renamed.previous_path == "old/name.py"


# -- publishing ------------------------------------------------------------


def make_result() -> ReviewResult:
    from roborak.context.diff import parse_diff

    files = parse_diff(
        f"diff --git a/app/auth.py b/app/auth.py\n--- a/app/auth.py\n+++ b/app/auth.py\n{PATCH}"
    )
    changeset = ChangeSet(
        files=files,
        origin="gitlab",
        head_sha="head333",
        forge_ref=ForgeRef(
            provider="gitlab",
            host="gitlab.com",
            project="acme/web",
            number=298,
            base_sha="base111",
            start_sha="start222",
            head_sha="head333",
        ),
    )
    finding = Finding(
        file="app/auth.py",
        start_line=11,
        end_line=11,
        severity=Severity.CRITICAL,
        category=Category.SECURITY,
        kind=Kind.POTENTIAL_ISSUE,
        title="SQL injection",
        body="user_id is concatenated into SQL.",
        suggestion="    row = db.execute('SELECT * FROM s WHERE u = ?', (user_id,))",
    )
    return ReviewResult(findings=[finding], changeset=changeset, model="test/model")


def test_gitlab_position_payload_uses_all_three_shas(monkeypatch):
    """The whole reason GitLab publishing is delicate."""
    posted: list[tuple[str, dict]] = []
    target = Target("gitlab", "gitlab.com", "acme/web", 298)

    def handler(request: httpx.Request) -> httpx.Response:
        posted.append((request.url.path, json.loads(request.content)))
        return httpx.Response(201, json={"id": "1"})

    monkeypatch.setattr(
        "roborak.publish.gitlab.ForgeClient", lambda t, tok: client_with(handler, t)
    )
    report = GitLabPublisher(target=target, token="tok").publish(make_result())

    assert len(report.posted) == 1
    discussion = next(body for path, body in posted if path.endswith("/discussions"))
    assert discussion["position[base_sha]"] == "base111"
    assert discussion["position[start_sha]"] == "start222"
    assert discussion["position[head_sha]"] == "head333"
    assert discussion["position[new_path]"] == "app/auth.py"
    assert discussion["position[new_line]"] == 11
    assert discussion["position[position_type]"] == "text"
    assert "```suggestion" in discussion["body"]

    assert report.summary_posted
    assert any(path.endswith("/notes") for path, _ in posted)


def test_gitlab_skips_findings_it_cannot_anchor(monkeypatch):
    target = Target("gitlab", "gitlab.com", "acme/web", 298)
    monkeypatch.setattr(
        "roborak.publish.gitlab.ForgeClient",
        lambda t, tok: client_with(lambda r: httpx.Response(201, json={}), t),
    )
    result = make_result()
    result.findings[0].start_line = 900  # nowhere near the diff

    report = GitLabPublisher(target=target, token="tok").publish(result)
    assert report.posted == []
    assert len(report.failed) == 1
    assert "anchor" in report.failed[0][1]


def test_gitlab_one_rejected_comment_does_not_abort_the_review(monkeypatch):
    target = Target("gitlab", "gitlab.com", "acme/web", 298)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/discussions"):
            return httpx.Response(400, text="bad position")
        return httpx.Response(201, json={})

    monkeypatch.setattr(
        "roborak.publish.gitlab.ForgeClient", lambda t, tok: client_with(handler, t)
    )
    report = GitLabPublisher(target=target, token="tok").publish(make_result())
    assert report.posted == []
    assert len(report.failed) == 1
    assert report.summary_posted, "the summary must still land"


def test_github_review_is_submitted_as_one_call(monkeypatch):
    posted: list[dict] = []
    target = Target("github", "github.com", "acme/web", 42)

    def handler(request: httpx.Request) -> httpx.Response:
        posted.append(json.loads(request.content))
        return httpx.Response(200, json={"id": 1})

    monkeypatch.setattr(
        "roborak.publish.github.ForgeClient", lambda t, tok: client_with(handler, t)
    )
    report = GitHubPublisher(target=target, token="tok").publish(make_result())

    assert len(posted) == 1, "one review, not one comment per finding"
    payload = posted[0]
    assert payload["event"] == "COMMENT", "must never approve on the user's behalf"
    assert payload["commit_id"] == "head333"
    comment = payload["comments"][0]
    assert comment["path"] == "app/auth.py"
    assert comment["line"] == 11
    assert comment["side"] == "RIGHT"
    assert "```suggestion" in comment["body"]
    assert len(report.posted) == 1


def test_github_falls_back_to_a_plain_comment_when_anchors_are_rejected(monkeypatch):
    target = Target("github", "github.com", "acme/web", 42)
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path.endswith("/reviews"):
            return httpx.Response(422, text="line must be part of the diff")
        return httpx.Response(201, json={})

    monkeypatch.setattr(
        "roborak.publish.github.ForgeClient", lambda t, tok: client_with(handler, t)
    )
    report = GitHubPublisher(target=target, token="tok").publish(make_result())

    assert any(p.endswith("/issues/42/comments") for p in paths), "summary must still land"
    assert report.posted == []
    assert len(report.failed) == 1


def test_publishing_a_local_review_is_refused():
    target = Target("gitlab", "gitlab.com", "acme/web", 298)
    result = ReviewResult(changeset=ChangeSet(origin="local"))
    with pytest.raises(SourceError, match="did not come from"):
        GitLabPublisher(target=target, token="tok").publish(result)


# -- markdown --------------------------------------------------------------


def test_finding_markdown_contains_everything_a_reviewer_needs():
    result = make_result()
    markdown = finding_markdown(result.findings[0])
    assert "Critical" in markdown
    assert "SQL injection" in markdown
    assert "```suggestion" in markdown
    assert "security" in markdown


def test_summary_markdown_has_a_severity_table():
    markdown = summary_markdown(make_result())
    assert "## roborak review" in markdown
    assert "| Severity | Count |" in markdown
    assert "test/model" in markdown


def test_summary_markdown_when_clean():
    assert "No findings" in summary_markdown(ReviewResult())


# -- state -----------------------------------------------------------------


def test_state_round_trip(tmp_path: Path):
    store = StateStore(tmp_path)
    key = review_key("gitlab", "gitlab.com", "acme/web", 298)
    assert store.get(key).fingerprints == set()

    findings = make_result().findings
    store.record(key, findings, "head333")

    reloaded = StateStore(tmp_path).get(key)
    assert reloaded.fingerprints == {f.fingerprint for f in findings}
    assert reloaded.last_head_sha == "head333"
    assert reloaded.last_reviewed_at


def test_state_accumulates_across_runs(tmp_path: Path):
    store = StateStore(tmp_path)
    key = review_key("gitlab", "gitlab.com", "acme/web", 298)
    first = make_result().findings
    store.record(key, first, "sha1")

    second = make_result().findings
    second[0].body = "A different problem entirely."
    store.record(key, second, "sha2")

    assert len(store.get(key).fingerprints) == 2
    assert store.get(key).last_head_sha == "sha2"


def test_already_posted_findings_are_skipped(monkeypatch, tmp_path: Path):
    """The behaviour that stops a bot spamming a PR on every push."""
    target = Target("gitlab", "gitlab.com", "acme/web", 298)
    monkeypatch.setattr(
        "roborak.publish.gitlab.ForgeClient",
        lambda t, tok: client_with(lambda r: httpx.Response(201, json={}), t),
    )
    result = make_result()
    seen = frozenset({result.findings[0].fingerprint})

    report = GitLabPublisher(target=target, token="tok", seen_fingerprints=seen).publish(result)
    assert report.posted == []
    assert len(report.skipped_duplicate) == 1


def test_corrupt_state_is_survivable(tmp_path: Path):
    store = StateStore(tmp_path)
    store.path.parent.mkdir(parents=True)
    store.path.write_text("{not json at all")
    assert store.get("anything").fingerprints == set()


def test_state_can_be_cleared(tmp_path: Path):
    store = StateStore(tmp_path)
    key = review_key("gitlab", "gitlab.com", "acme/web", 298)
    store.record(key, make_result().findings, "sha")
    store.clear(key)
    assert store.get(key).fingerprints == set()
