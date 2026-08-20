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

from roborak.core.config import ForgeConfig, load_config
from roborak.core.models import ChangeSet, Finding, ForgeRef, Issue, ReviewResult
from roborak.core.severity import Category, Kind, Severity
from roborak.publish.base import finding_markdown, summary_markdown
from roborak.publish.github import GitHubPublisher
from roborak.publish.gitlab import GitLabPublisher
from roborak.sources.base import SourceError
from roborak.sources.forge import (
    ForgeClient,
    Target,
    get_token,
    parse_target,
    project_from_remote,
    resolve_host,
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


def test_remote_host_beats_a_configured_one(monkeypatch):
    monkeypatch.setattr("roborak.sources.forge.detect_host", lambda *a, **k: "gitlab.com")
    forge = ForgeConfig(hosts={"gitlab": "gitlab.acme.com"})
    # Per-repo evidence wins: a user-wide domain must not hijack this checkout.
    assert resolve_host("gitlab", forge) == "gitlab.com"


def test_configured_host_fills_in_when_there_is_no_remote(monkeypatch):
    monkeypatch.setattr("roborak.sources.forge.detect_host", lambda *a, **k: None)
    assert resolve_host("gitlab", ForgeConfig(hosts={"gitlab": "gitlab.acme.com"})) == (
        "gitlab.acme.com"
    )
    assert resolve_host("github", ForgeConfig(hosts={"gitlab": "gitlab.acme.com"})) is None
    assert resolve_host("gitlab") is None


def test_a_configured_host_targets_a_bare_number():
    target = parse_target("705", "gitlab", host="gitlab.acme.com", project="acme/app")
    assert target.host == "gitlab.acme.com"
    assert target.api_base == "https://gitlab.acme.com/api/v4"


def test_a_plain_http_host_keeps_its_scheme_and_port():
    target = parse_target("705", "gitlab", host="http://gitlab.local:8080", project="a/b")
    # The scheme stays off `host`, which doubles as a state key and error label.
    assert (target.host, target.scheme) == ("gitlab.local:8080", "http")
    assert target.api_base == "http://gitlab.local:8080/api/v4"


def test_an_http_url_is_not_silently_upgraded():
    target = parse_target("http://gitlab.local:8080/a/b/-/merge_requests/7", "gitlab")
    assert target.api_base == "http://gitlab.local:8080/api/v4"


def test_an_http_remote_keeps_its_scheme(monkeypatch):
    monkeypatch.setattr(
        "roborak.sources.forge._remote_url", lambda *a, **k: "http://gl.local:8080/a/b.git"
    )
    assert resolve_host("gitlab") == "http://gl.local:8080"


def test_configured_token_is_used_and_beats_the_environment(monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN", "from-env")
    forge = ForgeConfig(tokens={"gitlab": "from-config"})
    assert get_token("gitlab", forge) == "from-config"


def test_token_falls_back_to_the_environment_for_unconfigured_providers(monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN", "from-env")
    assert get_token("gitlab", ForgeConfig(tokens={"github": "gh"})) == "from-env"
    assert get_token("gitlab") == "from-env"


def test_roborak_prefixed_env_var_reaches_the_forge_config(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("roborak.core.config.USER_CONFIG_PATH", tmp_path / "absent.yaml")
    monkeypatch.setenv("ROBORAK_GITLAB_TOKEN", "from-env")
    (tmp_path / ".roborak.yaml").write_text("forge:\n  tokens:\n    gitlab: from-file\n")
    # The environment is the higher layer, so it still wins over the file.
    assert get_token("gitlab", load_config(tmp_path).forge) == "from-env"


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


def test_gitlab_recovers_truncated_text_and_classifies_binary():
    from roborak.sources.gitlab import _files_from_changes

    class Client:
        def get_raw(self, path, **params):
            if path.endswith("binary.bin/raw"):
                return b"\0binary"
            return b"x = 1\n" if params["ref"] == "base" else b"x = 1\ny = 2\n"

    target = Target("gitlab", "gitlab.com", "acme/web", 42)
    files = _files_from_changes(
        [
            {"old_path": "app.py", "new_path": "app.py", "diff": ""},
            {"old_path": "binary.bin", "new_path": "binary.bin", "diff": ""},
            {"old_path": "", "new_path": "", "diff": ""},
        ],
        client=Client(),
        target=target,
        base_sha="base",
        head_sha="head",
    )
    assert files[0].added_lines == {2}
    assert files[1].is_binary
    assert len(files) == 2


def test_gitlab_marks_a_failed_or_oversized_recovery_unavailable():
    from roborak.sources.gitlab import _files_from_changes

    class Client:
        def get_raw(self, path, **params):
            return b"too large"

    target = Target("gitlab", "gitlab.com", "acme/web", 42)
    [file] = _files_from_changes(
        [{"old_path": "app.py", "new_path": "app.py", "diff": ""}],
        client=Client(),
        target=target,
        base_sha="base",
        head_sha="head",
        max_bytes=2,
    )
    assert file.patch_unavailable

    [without_client] = _files_from_changes(
        [{"old_path": "app.py", "new_path": "app.py", "diff": ""}]
    )
    assert without_client.is_binary


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


def test_github_recovers_a_truncated_text_patch():
    import base64

    from roborak.sources.github import _to_changed_file

    class Client:
        def get(self, path, **params):
            content = "x = 1\n" if params["ref"] == "base" else "x = 1\ny = 2\n"
            return {"encoding": "base64", "content": base64.b64encode(content.encode()).decode()}

    target = Target("github", "github.com", "acme/web", 42)
    file = _to_changed_file(
        {"filename": "app.py", "status": "modified"}, Client(), target, "base", "head"
    )
    assert not file.patch_unavailable
    assert file.added_lines == {2}


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


def test_a_finding_it_cannot_anchor_is_reported_not_dropped(monkeypatch):
    """It used to be counted as a failure and shown only in the terminal, so a
    reviewer reading the merge request never learned it existed."""
    posted: list[tuple[str, dict]] = []
    target = Target("gitlab", "gitlab.com", "acme/web", 298)

    def handler(request: httpx.Request) -> httpx.Response:
        posted.append((request.url.path, json.loads(request.content)))
        return httpx.Response(201, json={"id": "1"})

    monkeypatch.setattr(
        "roborak.publish.gitlab.ForgeClient", lambda t, tok: client_with(handler, t)
    )
    result = make_result()
    result.findings[0].start_line = 900  # nowhere near the diff

    report = GitLabPublisher(target=target, token="tok").publish(result)

    assert report.posted == []
    assert report.failed == [], "not being anchorable is not a failure to post"
    assert [f.title for f in report.summarised] == ["SQL injection"]

    note = next(body for path, body in posted if path.endswith("/notes"))
    assert "[!CAUTION]" in note["body"]
    assert "Outside diff range comments (1)" in note["body"]
    assert "SQL injection" in note["body"]


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


def test_github_no_summary_never_posts_a_fallback_comment(monkeypatch):
    target = Target("github", "github.com", "acme/web", 42)
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(422, text="invalid anchor")

    monkeypatch.setattr(
        "roborak.publish.github.ForgeClient", lambda t, tok: client_with(handler, t)
    )
    report = GitHubPublisher(target=target, token="tok", post_summary=False).publish(make_result())
    assert not any(path.endswith("/issues/42/comments") for path in paths)
    assert not report.summary_posted


def test_remote_markers_are_discovered_across_github_comment_surfaces(monkeypatch):
    from roborak.publish.base import remote_fingerprints

    target = Target("github", "github.com", "acme/web", 42)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[{"body": "<!-- roborak:v1:0123456789abcdef -->"}],
        )

    monkeypatch.setattr("roborak.publish.base.ForgeClient", lambda t, tok: client_with(handler, t))
    assert remote_fingerprints(target, "tok") == {"0123456789abcdef"}


def test_remote_markers_are_discovered_in_gitlab_discussions(monkeypatch):
    from roborak.publish.base import remote_fingerprints

    target = Target("gitlab", "gitlab.com", "acme/web", 42)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/discussions"):
            return httpx.Response(
                200,
                json=[{"notes": [{"body": "<!-- roborak:v2:fedcba9876543210 -->"}]}],
            )
        return httpx.Response(200, json=[])

    monkeypatch.setattr("roborak.publish.base.ForgeClient", lambda t, tok: client_with(handler, t))
    assert remote_fingerprints(target, "tok") == {"fedcba9876543210"}


def test_publishing_a_local_review_is_refused():
    target = Target("gitlab", "gitlab.com", "acme/web", 298)
    result = ReviewResult(changeset=ChangeSet(origin="local"))
    with pytest.raises(SourceError, match="did not come from"):
        GitLabPublisher(target=target, token="tok").publish(result)


# -- markdown --------------------------------------------------------------


def test_finding_markdown_contains_everything_a_reviewer_needs():
    result = make_result()
    body = finding_markdown(result.findings[0])
    assert "_🔒 Security_ | _🔴 Critical_" in body
    assert "**SQL injection.**" in body
    assert "```suggestion" in body
    # An inline comment carries the same agent prompt and identity as the report.
    assert "🤖 Prompt for AI Agents" in body
    assert f"<!-- roborak:v1:{result.findings[0].fingerprint} -->" in body


def test_gitlabs_ranged_suggestion_fence_survives_the_shared_renderer():
    result = make_result()
    result.findings[0].end_line = result.findings[0].start_line + 2
    body = finding_markdown(result.findings[0], suggestion_syntax="suggestion:-0+2")
    assert "```suggestion:-0+2" in body


def test_the_comment_is_the_whole_report():
    """The single most important invariant of this shape: what was printed on
    screen is byte for byte what lands on the merge request."""
    from roborak.render import markdown as markdown_render

    result = make_result()
    assert summary_markdown(result) == markdown_render.render(result)


def test_the_comment_repeats_the_findings_that_also_went_inline():
    """A deliberate trade: a comment that omitted them would be a document
    nobody had read before it was published."""
    body = summary_markdown(make_result())
    assert "**SQL injection.**" in body
    assert "No findings" not in body


def test_summary_markdown_carries_what_could_not_go_inline():
    result = make_result()
    result.findings[0].start_line = 900  # nowhere near the diff
    body = summary_markdown(result)
    assert "| Severity | Count |" in body
    assert "Outside diff range comments (1)" in body
    assert "**Model**: `test/model`" in body


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
    assert reloaded.fingerprints == {
        identity
        for finding in findings
        for identity in (finding.fingerprint, finding.fingerprint_v2)
    }
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

    # The body-based v1 changes, while the wording-resistant v2 remains stable.
    assert len(store.get(key).fingerprints) == 3
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


# -- requirement gaps ------------------------------------------------------

GAP = Finding(
    file="app/auth.py",
    start_line=1,
    end_line=1,
    severity=Severity.MAJOR,
    category=Category.SECURITY,
    kind=Kind.REQUIREMENT_GAP,
    title="Rate limiting was never added",
    body="The issue asks for a rate limit on this endpoint; nothing here adds one.",
)


def result_with_a_gap() -> ReviewResult:
    result = make_result()
    result.findings.append(GAP)
    result.issue = Issue(
        provider="gitlab", host="gitlab.com", project="acme/web", number=42, title="Harden auth"
    )
    return result


def test_gitlab_posts_a_gap_in_the_summary_not_inline(monkeypatch):
    posted: list[tuple[str, dict]] = []
    target = Target("gitlab", "gitlab.com", "acme/web", 298)

    def handler(request: httpx.Request) -> httpx.Response:
        posted.append((request.url.path, json.loads(request.content)))
        return httpx.Response(201, json={"id": "1"})

    monkeypatch.setattr(
        "roborak.publish.gitlab.ForgeClient", lambda t, tok: client_with(handler, t)
    )
    report = GitLabPublisher(target=target, token="tok").publish(result_with_a_gap())

    # The gap has no honest line, so it must not become an inline discussion.
    assert [f.title for f in report.posted] == ["SQL injection"]
    assert [f.title for f in report.summarised] == ["Rate limiting was never added"]
    assert report.failed == []

    note = next(body for path, body in posted if path.endswith("/notes"))
    assert "🔍 Requirements not met (1)" in note["body"]
    assert "Rate limiting was never added" in note["body"]


def test_github_leaves_a_gap_out_of_the_inline_comments(monkeypatch):
    posted: list[dict] = []
    target = Target("github", "github.com", "acme/web", 42)

    def handler(request: httpx.Request) -> httpx.Response:
        posted.append(json.loads(request.content))
        return httpx.Response(200, json={"id": 1})

    monkeypatch.setattr(
        "roborak.publish.github.ForgeClient", lambda t, tok: client_with(handler, t)
    )
    report = GitHubPublisher(target=target, token="tok").publish(result_with_a_gap())

    (payload,) = posted
    assert len(payload["comments"]) == 1
    assert [f.title for f in report.summarised] == ["Rate limiting was never added"]
    assert "Rate limiting was never added" in payload["body"]


def test_summary_without_gaps_has_no_requirements_section():
    assert "Requirements not met" not in summary_markdown(make_result())


# -- the shared anchorability check ----------------------------------------


def test_can_anchor_agrees_with_what_the_publishers_do():
    """One check, so the pre-flight preview and the publishers cannot disagree."""
    from roborak.core.buckets import can_anchor

    result = make_result()
    changeset = result.changeset
    assert changeset is not None
    finding = result.findings[0]

    assert can_anchor(finding, changeset)

    finding.start_line = 900  # nowhere near the diff
    assert not can_anchor(finding, changeset)

    finding.start_line = 11
    finding.file = "app/never_touched.py"
    assert not can_anchor(finding, changeset)


def test_post_inline_false_publishes_only_the_summary(monkeypatch):
    """What the prompt's [s] answer does."""
    posted: list[str] = []
    target = Target("gitlab", "gitlab.com", "acme/web", 298)

    def handler(request: httpx.Request) -> httpx.Response:
        posted.append(request.url.path)
        return httpx.Response(201, json={"id": "1"})

    monkeypatch.setattr(
        "roborak.publish.gitlab.ForgeClient", lambda t, tok: client_with(handler, t)
    )
    report = GitLabPublisher(target=target, token="tok", post_inline=False).publish(make_result())

    assert report.posted == []
    assert report.summary_posted
    assert not any(path.endswith("/discussions") for path in posted)
    assert any(path.endswith("/notes") for path in posted)
