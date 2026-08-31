"""Forge sources and publishers.

The assertions that matter are on the *position payloads*: getting these wrong
puts review comments on unrelated lines of somebody's merge request, which is the
single worst thing this tool can do.
"""

from __future__ import annotations

import json
from collections.abc import Callable
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
    assert get_token("gitlab", load_config(tmp_path).forge) == "from-env"


def test_gitlab_encodes_the_project_path():
    assert Target("gitlab", "gitlab.com", "group/sub/app", 1).encoded_project == "group%2Fsub%2Fapp"


def test_api_base_handles_enterprise_hosts():
    assert Target("github", "github.com", "a/b", 1).api_base == "https://api.github.com"
    assert Target("github", "gh.corp", "a/b", 1).api_base == "https://gh.corp/api/v3"
    assert Target("gitlab", "gl.corp", "a/b", 1).api_base == "https://gl.corp/api/v4"


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


def test_gitlab_treats_a_zero_byte_file_as_empty_rather_than_unavailable():
    """A ``.gitkeep`` has no patch because it has no content, and that is not a failure."""
    from roborak.sources.gitlab import _files_from_changes

    class Client:
        def get_raw(self, path, **params):
            return b""

    target = Target("gitlab", "gitlab.com", "acme/web", 42)
    [file] = _files_from_changes(
        [{"old_path": "", "new_path": "routes/public/.gitkeep", "diff": "", "new_file": True}],
        client=Client(),
        target=target,
        base_sha="base",
        head_sha="head",
    )
    assert file.zero_byte
    assert not file.patch_unavailable
    assert not file.is_binary


def test_github_treats_a_zero_byte_file_as_empty_rather_than_unavailable():
    import base64

    from roborak.sources.github import _to_changed_file

    class Client:
        def get(self, path, **params):
            return {"encoding": "base64", "content": base64.b64encode(b"").decode()}

    target = Target("github", "github.com", "acme/web", 42)
    file = _to_changed_file(
        {"filename": "routes/public/.gitkeep", "status": "added"},
        Client(),
        target,
        "base",
        "head",
    )
    assert file.zero_byte
    assert not file.patch_unavailable
    assert not file.is_binary


GITHUB_PR = {
    "title": "Add session lookup",
    "body": "Adds a session cache.",
    "html_url": "https://github.com/acme/web/pull/42",
    "base": {"sha": "base111", "ref": "main"},
    "head": {"sha": "head333", "ref": "feature"},
}
GITHUB_FILES = [
    {"filename": "app/auth.py", "status": "modified", "patch": PATCH},
    {"filename": "logo.png", "status": "modified"},
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
    result.findings[0].start_line = 900

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
    report = GitHubPublisher(target=target, token="tok", post_check=False).publish(make_result())

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
        if request.url.path.endswith("/user"):
            return httpx.Response(403, text="job tokens may not")
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
        if request.url.path.endswith("/user"):
            return httpx.Response(403, text="job tokens may not")
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


def test_finding_markdown_contains_everything_a_reviewer_needs():
    result = make_result()
    body = finding_markdown(result.findings[0])
    assert body.startswith("**🔴 CRITICAL** · `11-11`")
    assert "_🔒 Security · 🔨 Moderate_" in body
    assert "[!CAUTION]" not in body, "an inline thread is one finding; it needs no alert box"
    assert "**SQL injection.**" in body
    assert "```suggestion" in body
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
    result.findings[0].start_line = 900
    body = summary_markdown(result)
    assert "| Severity | Count |" in body
    assert "Outside diff range comments (1)" in body
    assert "**Model**" not in body
    assert "Model usage" not in body


def test_summary_markdown_when_clean():
    assert "No findings" in summary_markdown(ReviewResult())


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

    assert [f.title for f in report.posted] == ["SQL injection"]
    assert [f.title for f in report.summarised] == ["Rate limiting was never added"]
    assert report.failed == []

    note = next(body for path, body in posted if path.endswith("/notes"))
    assert "📋 Requirements not met (1)" in note["body"]
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
    report = GitHubPublisher(target=target, token="tok", post_check=False).publish(
        result_with_a_gap()
    )

    (payload,) = posted
    assert len(payload["comments"]) == 1
    assert [f.title for f in report.summarised] == ["Rate limiting was never added"]
    assert "Rate limiting was never added" in payload["body"]


def test_summary_without_gaps_has_no_requirements_section():
    assert "Requirements not met" not in summary_markdown(make_result())


def test_can_anchor_agrees_with_what_the_publishers_do():
    """One check, so the pre-flight preview and the publishers cannot disagree."""
    from roborak.core.buckets import can_anchor

    result = make_result()
    changeset = result.changeset
    assert changeset is not None
    finding = result.findings[0]

    assert can_anchor(finding, changeset)

    finding.start_line = 900
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


# --- reusing an overview that is already published ------------------------------


def summary_body(result: ReviewResult) -> str:
    """What an earlier run would have left on the merge request."""
    return summary_markdown(result)


def test_flow_digest_ignores_what_a_hunk_says_and_notices_where_it_is():
    """The overview narrates the shape of a change, not its every character."""
    result = make_result()
    changeset = result.changeset
    assert changeset is not None
    original = changeset.flow_digest

    changeset.files[0].hunks[0].content += "\n+# an afterthought"
    assert changeset.flow_digest == original

    changeset.files[0].hunks[0].new_start += 40
    assert changeset.flow_digest != original


def test_an_empty_changeset_has_no_flow_to_digest():
    assert ChangeSet().flow_digest == ""


def test_the_published_summary_carries_the_shape_it_describes():
    result = make_result()
    assert result.changeset is not None
    assert f"<!-- roborak:flow:{result.changeset.flow_digest} -->" in summary_markdown(result)
    assert "<!-- roborak:review -->" in summary_markdown(result)


def test_remote_state_finds_the_summary_note_on_gitlab(monkeypatch):
    from roborak.publish.base import remote_state

    target = Target("gitlab", "gitlab.com", "acme/web", 298)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/user"):
            return httpx.Response(403, text="job tokens may not")
        if request.url.path.endswith("/notes"):
            return httpx.Response(
                200,
                json=[
                    {"id": 7, "body": "a human said something"},
                    {
                        "id": 9,
                        "body": summary_body(make_result()),
                        "author": {"username": "roborak-bot", "bot": True},
                    },
                ],
            )
        return httpx.Response(200, json=[])

    monkeypatch.setattr("roborak.publish.base.ForgeClient", lambda t, tok: client_with(handler, t))
    state = remote_state(target, "tok")

    assert state.summary is not None
    assert state.summary.method == "PUT"
    assert state.summary.edit_path.endswith("/merge_requests/298/notes/9")
    assert make_result().changeset is not None
    assert state.summary.flow == make_result().changeset.flow_digest
    assert state.fingerprints  # the inline identities still come back


def test_remote_state_finds_the_summary_review_on_github(monkeypatch):
    from roborak.publish.base import remote_state

    target = Target("github", "github.com", "acme/web", 42)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/user"):
            return httpx.Response(403, text="job tokens may not")
        if request.url.path.endswith("/pulls/42/reviews"):
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 55,
                        "body": summary_body(make_result()),
                        "user": {"login": "roborak-bot", "type": "Bot"},
                    }
                ],
            )
        return httpx.Response(200, json=[])

    monkeypatch.setattr("roborak.publish.base.ForgeClient", lambda t, tok: client_with(handler, t))
    state = remote_state(target, "tok")

    assert state.summary is not None
    assert state.summary.method == "PUT"
    assert state.summary.edit_path == "/repos/acme/web/pulls/42/reviews/55"


def test_a_published_overview_survives_the_round_trip_through_its_comment():
    """The marker is the whole point: it is what a second machine reads."""
    from roborak.core.models import FileSummary, Walkthrough
    from roborak.render.markdown import decode_walkthrough, encode_walkthrough

    walkthrough = Walkthrough(
        title="Publish empty reviews",
        overview="Sends the summary through even when nothing was found.",
        file_summaries=[FileSummary(path="app/auth.py", summary="Guards the session lookup.")],
        sequence_diagram="flowchart TD\n  A --> B",
        labels=["bugfix"],
        estimated_effort=3,
    )

    recovered = decode_walkthrough(encode_walkthrough(walkthrough))

    assert recovered == walkthrough


def test_an_overview_marker_never_closes_the_comment_it_rides_in():
    """A raw JSON overview could contain ``-->`` and end the marker early."""
    from roborak.core.models import Walkthrough
    from roborak.render.markdown import encode_walkthrough

    token = encode_walkthrough(Walkthrough(overview="a --> b, and <!-- not a marker -->"))

    assert "-->" not in token and "<!--" not in token


@pytest.mark.parametrize(
    "token",
    ["", "not base64!", "bm90IGpzb24=", "eyJ0aXRsZSI6IFtdfQ=="],
    ids=["absent", "unreadable", "not-json", "wrong-schema"],
)
def test_an_unreadable_overview_marker_reads_as_no_overview(token):
    """Anything we cannot trust is re-narrated rather than published wrong."""
    from roborak.render.markdown import decode_walkthrough

    assert decode_walkthrough(token) is None


def test_an_overview_too_large_to_ride_along_is_left_behind():
    """The comment has a size limit; the copy gives way before the review does."""
    import secrets

    from roborak.core.models import Walkthrough
    from roborak.render.markdown import MAX_WALKTHROUGH_MARKER, encode_walkthrough

    incompressible = Walkthrough(overview=secrets.token_hex(MAX_WALKTHROUGH_MARKER))

    assert encode_walkthrough(incompressible) == "", "dropped rather than truncated"


def test_a_zip_bomb_in_the_marker_never_gets_room_to_expand():
    """The token comes off a comment anyone can edit, so the decoder bounds it."""
    import base64
    import zlib

    from roborak.render.markdown import MAX_WALKTHROUGH_PAYLOAD, decode_walkthrough

    bomb = base64.b64encode(zlib.compress(b"\0" * (MAX_WALKTHROUGH_PAYLOAD * 4), 9)).decode()

    assert len(bomb) < 8192, "a few kilobytes standing for many megabytes"
    assert decode_walkthrough(bomb) is None


def test_an_oversized_marker_is_rejected_before_it_is_decoded():
    """Nothing honest reaches the encoder's ceiling, so nothing past it is read."""
    from roborak.render.markdown import MAX_WALKTHROUGH_MARKER, decode_walkthrough

    assert decode_walkthrough("A" * (MAX_WALKTHROUGH_MARKER + 4)) is None


def test_a_realistic_overview_is_a_small_fraction_of_the_comment_budget():
    """Prose deflates well, so carrying it costs the review almost nothing."""
    from roborak.core.models import FileSummary, Walkthrough
    from roborak.render.markdown import MAX_WALKTHROUGH_MARKER, encode_walkthrough

    big = Walkthrough(
        title="A pull request touching a great many files",
        overview="word " * 200,
        file_summaries=[
            FileSummary(path=f"src/pkg/module_{i}/file_{i}.py", summary="sentence " * 15)
            for i in range(40)
        ],
        sequence_diagram="flowchart TD\n" + "\n".join(f"  N{i} --> N{i + 1}" for i in range(60)),
        labels=["bugfix", "tests"],
        estimated_effort=5,
    )

    assert 0 < len(encode_walkthrough(big)) < MAX_WALKTHROUGH_MARKER // 4


def test_remote_state_reads_the_overview_back_off_the_published_review(monkeypatch):
    """What #23 came down to: this is the copy CI can actually reach."""
    from roborak.core.models import Walkthrough
    from roborak.publish.base import remote_state

    target = Target("github", "github.com", "acme/web", 42)
    result = make_result()
    result.walkthrough = Walkthrough(overview="Looks up a session row.")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/user"):
            return httpx.Response(403, text="job tokens may not")
        if request.url.path.endswith("/pulls/42/reviews"):
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 55,
                        "body": summary_body(result),
                        "user": {"login": "roborak-bot", "type": "Bot"},
                    }
                ],
            )
        return httpx.Response(200, json=[])

    monkeypatch.setattr("roborak.publish.base.ForgeClient", lambda t, tok: client_with(handler, t))
    summary = remote_state(target, "tok").summary

    assert summary is not None and summary.walkthrough is not None
    assert summary.walkthrough.overview == "Looks up a session row."


def test_a_review_published_without_an_overview_carries_no_marker(monkeypatch):
    """--no-llm never had an overview; it must not claim an empty one."""
    from roborak.publish.base import remote_state

    target = Target("github", "github.com", "acme/web", 42)
    result = make_result()
    result.walkthrough = None

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/user"):
            return httpx.Response(403, text="job tokens may not")
        if request.url.path.endswith("/pulls/42/reviews"):
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 55,
                        "body": summary_body(result),
                        "user": {"login": "roborak-bot", "type": "Bot"},
                    }
                ],
            )
        return httpx.Response(200, json=[])

    monkeypatch.setattr("roborak.publish.base.ForgeClient", lambda t, tok: client_with(handler, t))
    summary = remote_state(target, "tok").summary

    assert summary is not None and summary.walkthrough is None


def test_remote_state_finds_the_summary_as_a_github_issue_comment(monkeypatch):
    from roborak.publish.base import remote_state

    target = Target("github", "github.com", "acme/web", 42)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/user"):
            return httpx.Response(403, text="job tokens may not")
        if request.url.path.endswith("/issues/42/comments"):
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 31,
                        "body": summary_body(make_result()),
                        "user": {"login": "roborak-bot", "type": "Bot"},
                    }
                ],
            )
        return httpx.Response(200, json=[])

    monkeypatch.setattr("roborak.publish.base.ForgeClient", lambda t, tok: client_with(handler, t))
    state = remote_state(target, "tok")

    assert state.summary is not None
    assert state.summary.method == "PATCH"
    assert state.summary.edit_path == "/repos/acme/web/issues/comments/31"


def test_remote_state_reuses_the_newest_overview_on_gitlab(monkeypatch):
    """GitLab hands back notes newest-first, so traversal order is not recency."""
    from roborak.publish.base import remote_state

    target = Target("gitlab", "gitlab.com", "acme/web", 298)
    bot = {"username": "roborak-bot", "bot": True}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/user"):
            return httpx.Response(403, text="job tokens may not")
        if request.url.path.endswith("/notes"):
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 9,
                        "body": summary_body(make_result()),
                        "author": bot,
                        "created_at": "2026-03-02T10:00:00.000+01:00",
                    },
                    {
                        "id": 4,
                        "body": summary_body(make_result()),
                        "author": bot,
                        "created_at": "2026-03-01T10:00:00.000+01:00",
                    },
                ],
            )
        return httpx.Response(200, json=[])

    monkeypatch.setattr("roborak.publish.base.ForgeClient", lambda t, tok: client_with(handler, t))
    summary = remote_state(target, "tok").summary

    assert summary is not None
    assert summary.edit_path.endswith("/notes/9")


def test_remote_state_reuses_the_newest_overview_on_github(monkeypatch):
    """Reviews are swept after issue comments whatever their timestamps say."""
    from roborak.publish.base import remote_state

    target = Target("github", "github.com", "acme/web", 42)
    bot = {"login": "roborak-bot", "type": "Bot"}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/user"):
            return httpx.Response(403, text="job tokens may not")
        if request.url.path.endswith("/issues/42/comments"):
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 31,
                        "body": summary_body(make_result()),
                        "user": bot,
                        "created_at": "2026-03-02T10:00:00Z",
                    }
                ],
            )
        if request.url.path.endswith("/pulls/42/reviews"):
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 55,
                        "body": summary_body(make_result()),
                        "user": bot,
                        "submitted_at": "2026-03-01T10:00:00Z",
                    }
                ],
            )
        return httpx.Response(200, json=[])

    monkeypatch.setattr("roborak.publish.base.ForgeClient", lambda t, tok: client_with(handler, t))
    summary = remote_state(target, "tok").summary

    assert summary is not None
    assert summary.method == "PATCH"
    assert summary.edit_path == "/repos/acme/web/issues/comments/31"


def test_remote_state_trusts_only_the_publishing_account_on_github(monkeypatch):
    """The markers are public; authorship is what makes an overview roborak's."""
    from roborak.publish.base import remote_state

    target = Target("github", "github.com", "acme/web", 42)

    def handler_for(author: str):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/user":
                return httpx.Response(200, json={"login": "roborak-bot"})
            if request.url.path.endswith("/issues/42/comments"):
                return httpx.Response(
                    200,
                    json=[
                        {
                            "id": 31,
                            "body": summary_body(make_result()),
                            "user": {"login": author},
                        }
                    ],
                )
            return httpx.Response(200, json=[])

        return handler

    monkeypatch.setattr(
        "roborak.publish.base.ForgeClient",
        lambda t, tok: client_with(handler_for("roborak-bot"), t),
    )
    mine = remote_state(target, "tok").summary
    assert mine is not None
    assert mine.edit_path == "/repos/acme/web/issues/comments/31"

    monkeypatch.setattr(
        "roborak.publish.base.ForgeClient", lambda t, tok: client_with(handler_for("impostor"), t)
    )
    assert remote_state(target, "tok").summary is None


def test_remote_state_trusts_only_the_publishing_account_on_gitlab(monkeypatch):
    from roborak.publish.base import remote_state

    target = Target("gitlab", "gitlab.com", "acme/web", 298)

    def handler_for(author: str):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/v4/user":
                return httpx.Response(200, json={"username": "roborak-bot"})
            if request.url.path.endswith("/notes"):
                return httpx.Response(
                    200,
                    json=[
                        {
                            "id": 9,
                            "body": summary_body(make_result()),
                            "author": {"username": author},
                        }
                    ],
                )
            return httpx.Response(200, json=[])

        return handler

    monkeypatch.setattr(
        "roborak.publish.base.ForgeClient",
        lambda t, tok: client_with(handler_for("roborak-bot"), t),
    )
    assert remote_state(target, "tok").summary is not None

    monkeypatch.setattr(
        "roborak.publish.base.ForgeClient", lambda t, tok: client_with(handler_for("impostor"), t)
    )
    assert remote_state(target, "tok").summary is None


def _ci_note_handler(author: dict[str, object]):
    """A GitLab thread whose token cannot read ``/user``, holding one overview."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/user"):
            return httpx.Response(403, text="job tokens may not")
        if request.url.path.endswith("/notes"):
            return httpx.Response(
                200,
                json=[{"id": 9, "body": summary_body(make_result()), "author": author}],
            )
        return httpx.Response(200, json=[])

    return handler


def test_a_ci_token_that_cannot_name_itself_still_finds_its_overview(monkeypatch):
    """GitHub Actions and GitLab job tokens cannot read ``/user`` at all."""
    from roborak.publish.base import remote_state

    target = Target("gitlab", "gitlab.com", "acme/web", 298)
    handler = _ci_note_handler({"username": "ci", "bot": True})

    monkeypatch.setattr("roborak.publish.base.ForgeClient", lambda t, tok: client_with(handler, t))
    assert remote_state(target, "tok").summary is not None


def test_a_ci_token_ignores_an_overview_a_person_pasted(monkeypatch):
    """The markers are copyable; a contributor's account is not a bot account."""
    from roborak.publish.base import remote_state

    target = Target("gitlab", "gitlab.com", "acme/web", 298)
    handler = _ci_note_handler({"username": "impostor", "bot": False})

    monkeypatch.setattr("roborak.publish.base.ForgeClient", lambda t, tok: client_with(handler, t))
    assert remote_state(target, "tok").summary is None


def test_a_failing_user_lookup_does_not_weaken_the_ownership_check(monkeypatch):
    """A 5xx is the forge stumbling, not an answer; it must not trust the marker."""
    from roborak.publish.base import remote_state

    target = Target("gitlab", "gitlab.com", "acme/web", 298)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/user"):
            return httpx.Response(503, text="service unavailable")
        return httpx.Response(200, json=[])

    monkeypatch.setattr("roborak.publish.base.ForgeClient", lambda t, tok: client_with(handler, t))
    with pytest.raises(SourceError):
        remote_state(target, "tok")


def test_a_nameless_user_answer_does_not_weaken_the_ownership_check(monkeypatch):
    """A 200 that names nobody is a broken forge, not a token that may not ask."""
    from roborak.publish.base import remote_state

    target = Target("gitlab", "gitlab.com", "acme/web", 298)

    for payload in ([], {}, {"username": ""}):

        def handler(request: httpx.Request, payload=payload) -> httpx.Response:
            if request.url.path.endswith("/user"):
                return httpx.Response(200, json=payload)
            return httpx.Response(200, json=[])

        monkeypatch.setattr(
            "roborak.publish.base.ForgeClient", lambda t, tok: client_with(handler, t)
        )
        with pytest.raises(SourceError, match="without naming"):
            remote_state(target, "tok")


def test_remote_state_reports_no_summary_when_roborak_has_not_spoken(monkeypatch):
    from roborak.publish.base import remote_state

    target = Target("gitlab", "gitlab.com", "acme/web", 298)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/user"):
            return httpx.Response(200, json={"username": "roborak-bot"})
        return httpx.Response(200, json=[{"id": 1, "body": "looks good to me"}])

    monkeypatch.setattr("roborak.publish.base.ForgeClient", lambda t, tok: client_with(handler, t))
    assert remote_state(target, "tok").summary is None


def test_gitlab_edits_the_existing_summary_instead_of_appending_one(monkeypatch):
    from roborak.publish.base import SummaryRef

    target = Target("gitlab", "gitlab.com", "acme/web", 298)
    sent: list[tuple[str, str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append((request.method, request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"id": 9})

    monkeypatch.setattr(
        "roborak.publish.gitlab.ForgeClient", lambda t, tok: client_with(handler, t)
    )
    monkeypatch.setattr("roborak.publish.base.ForgeClient", lambda t, tok: client_with(handler, t))

    ref = SummaryRef(edit_path="/projects/acme%2Fweb/merge_requests/298/notes/9", method="PUT")
    report = GitLabPublisher(
        target=target, token="tok", summary_ref=ref, summary_refreshed=True
    ).publish(make_result())

    assert report.summary_updated and report.summary_posted
    edits = [(path, payload) for method, path, payload in sent if method == "PUT"]
    assert len(edits) == 1
    assert edits[0][0].endswith("/notes/9")
    assert edits[0][1]["body"].startswith("_Overview refreshed for `head333`")
    assert not any(path.endswith("/notes") for method, path, _ in sent if method == "POST")


def test_github_edits_the_summary_and_keeps_the_inline_review_separate(monkeypatch):
    from roborak.publish.base import SummaryRef

    target = Target("github", "github.com", "acme/web", 42)
    sent: list[tuple[str, str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append((request.method, request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"id": 55})

    monkeypatch.setattr(
        "roborak.publish.github.ForgeClient", lambda t, tok: client_with(handler, t)
    )
    monkeypatch.setattr("roborak.publish.base.ForgeClient", lambda t, tok: client_with(handler, t))

    ref = SummaryRef(edit_path="/repos/acme/web/pulls/42/reviews/55", method="PUT")
    report = GitHubPublisher(target=target, token="tok", summary_ref=ref).publish(make_result())

    assert report.summary_updated
    reviews = [p for m, path, p in sent if m == "POST" and path.endswith("/reviews")]
    assert len(reviews) == 1
    assert reviews[0]["body"] == ""  # the overview is not repeated in the review
    edits = [(path, p) for m, path, p in sent if m == "PUT"]
    assert len(edits) == 1
    assert edits[0][0] == "/repos/acme/web/pulls/42/reviews/55"
    assert not edits[0][1]["body"].startswith("_Overview refreshed")


def test_a_deleted_summary_comment_is_reposted_rather_than_lost(monkeypatch):
    from roborak.publish.base import SummaryRef

    target = Target("gitlab", "gitlab.com", "acme/web", 298)
    sent: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append((request.method, request.url.path))
        if request.method == "PUT":
            return httpx.Response(404, json={"message": "gone"})
        return httpx.Response(200, json={"id": 1})

    monkeypatch.setattr(
        "roborak.publish.gitlab.ForgeClient", lambda t, tok: client_with(handler, t)
    )
    ref = SummaryRef(edit_path="/projects/acme%2Fweb/merge_requests/298/notes/9", method="PUT")
    report = GitLabPublisher(target=target, token="tok", summary_ref=ref).publish(make_result())

    assert report.summary_posted
    assert not report.summary_updated
    assert ("POST", "/api/v4/projects/acme/web/merge_requests/298/notes") in sent


def test_state_written_before_the_overview_cache_still_reads(tmp_path):
    """Old state files predate the cache; they must not be thrown away."""
    from roborak.state.store import STATE_DIR, STATE_FILE, StateStore

    path = tmp_path / STATE_DIR / STATE_FILE
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "reviews": {"gitlab:gitlab.com:acme/web#298": {"fingerprints": ["abc123"]}},
            }
        )
    )

    record = StateStore(tmp_path).get("gitlab:gitlab.com:acme/web#298")
    assert record.fingerprints == {"abc123"}
    assert record.last_flow_digest == ""
    assert record.last_walkthrough is None


def test_a_new_flow_drops_the_overview_it_did_not_narrate(tmp_path):
    """Keeping it would serve the old change's narrative under the new digest."""
    from roborak.state.store import StateStore

    store = StateStore(tmp_path)
    key = "gitlab:gitlab.com:acme/web#298"
    store.record(key, [], "sha1", flow_digest="flowA", walkthrough={"summary": "old"})

    store.record(key, [], "sha2", flow_digest="flowB")

    record = store.get(key)
    assert record.last_flow_digest == "flowB"
    assert record.last_walkthrough is None


def test_an_unchanged_flow_keeps_its_cached_overview(tmp_path):
    from roborak.state.store import StateStore

    store = StateStore(tmp_path)
    key = "gitlab:gitlab.com:acme/web#298"
    store.record(key, [], "sha1", flow_digest="flowA", walkthrough={"summary": "old"})

    store.record(key, [], "sha2", flow_digest="flowA")

    assert store.get(key).last_walkthrough == {"summary": "old"}


# --- the pre-merge commit status -------------------------------------------


def status_calls(posted: list[tuple[str, dict]]) -> list[tuple[str, dict]]:
    return [(path, body) for path, body in posted if "/statuses/" in path]


def recording_handler(posted: list, status: int = 201, body: dict | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        posted.append((request.url.path, json.loads(request.content)))
        return httpx.Response(status, json=body if body is not None else {"id": "1"})

    return handler


def test_github_posts_the_verdict_as_a_commit_status(monkeypatch):
    posted: list[tuple[str, dict]] = []
    target = Target("github", "github.com", "acme/web", 42)
    monkeypatch.setattr(
        "roborak.publish.github.ForgeClient",
        lambda t, tok: client_with(recording_handler(posted), t),
    )
    result = make_result()
    result.block_on = Severity.CRITICAL

    report = GitHubPublisher(target=target, token="tok").publish(result)

    (path, body) = status_calls(posted)[0]
    assert path.endswith("/repos/acme/web/statuses/head333")
    assert body["state"] == "failure"
    assert body["context"] == "roborak/review"
    assert body["description"] == "1 finding at or above critical."
    assert report.status_posted
    assert report.status_skipped is None


def test_a_clean_review_posts_a_passing_status(monkeypatch):
    posted: list[tuple[str, dict]] = []
    target = Target("github", "github.com", "acme/web", 42)
    monkeypatch.setattr(
        "roborak.publish.github.ForgeClient",
        lambda t, tok: client_with(recording_handler(posted), t),
    )
    result = make_result()
    result.findings = []

    GitHubPublisher(target=target, token="tok").publish(result)

    assert status_calls(posted)[0][1]["state"] == "success"


def test_gitlab_posts_the_verdict_with_its_own_spelling_of_failure(monkeypatch):
    posted: list[tuple[str, dict]] = []
    wire: list[bytes] = []
    target = Target("gitlab", "gitlab.com", "acme/web", 298)

    def handler(request: httpx.Request) -> httpx.Response:
        posted.append((request.url.path, json.loads(request.content)))
        # httpx decodes %2F out of URL.path, and that is the one character a
        # GitLab project path must keep encoded, so check the wire form too.
        wire.append(request.url.raw_path.split(b"?")[0])
        return httpx.Response(201, json={"id": "1"})

    monkeypatch.setattr(
        "roborak.publish.gitlab.ForgeClient", lambda t, tok: client_with(handler, t)
    )
    result = make_result()
    result.block_on = Severity.CRITICAL

    report = GitLabPublisher(target=target, token="tok").publish(result)

    (path, body) = status_calls(posted)[0]
    assert path.endswith("/statuses/head333")
    assert wire[-1].endswith(b"/projects/acme%2Fweb/statuses/head333")
    assert body["state"] == "failed", "GitLab has no 'failure'"
    assert body["name"] == "roborak/review"
    assert report.status_posted


def test_the_status_context_is_stable_across_runs(monkeypatch):
    """Re-posting under one context replaces the status instead of stacking one."""
    posted: list[tuple[str, dict]] = []
    target = Target("github", "github.com", "acme/web", 42)
    monkeypatch.setattr(
        "roborak.publish.github.ForgeClient",
        lambda t, tok: client_with(recording_handler(posted), t),
    )
    for _ in range(2):
        GitHubPublisher(target=target, token="tok").publish(make_result())

    paths = [path for path, _ in status_calls(posted)]
    contexts = {body["context"] for _, body in status_calls(posted)}
    assert len(paths) == 2 and len(set(paths)) == 1
    assert contexts == {"roborak/review"}


def test_no_check_leaves_the_status_alone(monkeypatch):
    posted: list[tuple[str, dict]] = []
    target = Target("github", "github.com", "acme/web", 42)
    monkeypatch.setattr(
        "roborak.publish.github.ForgeClient",
        lambda t, tok: client_with(recording_handler(posted), t),
    )
    report = GitHubPublisher(target=target, token="tok", post_check=False).publish(make_result())

    assert status_calls(posted) == []
    assert not report.status_posted
    assert report.status_skipped is None


def test_an_under_scoped_token_still_publishes_the_review(monkeypatch):
    """A missing scope is reported, not fatal: the comments are the review."""
    posted: list[tuple[str, dict]] = []
    target = Target("github", "github.com", "acme/web", 42)

    def handler(request: httpx.Request) -> httpx.Response:
        posted.append((request.url.path, json.loads(request.content)))
        if "/statuses/" in request.url.path:
            return httpx.Response(403, text="Resource not accessible by personal access token")
        return httpx.Response(200, json={"id": 1})

    monkeypatch.setattr(
        "roborak.publish.github.ForgeClient", lambda t, tok: client_with(handler, t)
    )
    report = GitHubPublisher(target=target, token="tok").publish(make_result())

    assert len(report.posted) == 1, "the inline comment still went up"
    assert not report.status_posted
    assert "statuses:write" in (report.status_skipped or "")


def refusing_status_handler(existing: list[dict]) -> Callable:
    """A GitLab that refuses the status post and reports ``existing`` on the commit."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/statuses"):
            return httpx.Response(200, json=existing)
        if "/statuses/" in request.url.path:
            return httpx.Response(400, text="Cannot transition status")
        return httpx.Response(201, json={"id": "1"})

    return handler


def test_gitlab_treats_an_unchanged_status_as_already_correct(monkeypatch):
    """A refusal is forgiven only once the commit is seen already saying it."""
    target = Target("gitlab", "gitlab.com", "acme/web", 298)
    monkeypatch.setattr(
        "roborak.publish.gitlab.ForgeClient",
        lambda t, tok: client_with(
            refusing_status_handler([{"name": "roborak/review", "status": "failed"}]), t
        ),
    )
    report = GitLabPublisher(target=target, token="tok").publish(make_result())

    assert report.status_posted
    assert report.status_skipped is None


def test_a_refused_status_that_left_a_stale_verdict_is_reported(monkeypatch):
    """The message names a state machine, not the state the commit ended up in."""
    target = Target("gitlab", "gitlab.com", "acme/web", 298)
    monkeypatch.setattr(
        "roborak.publish.gitlab.ForgeClient",
        lambda t, tok: client_with(
            refusing_status_handler([{"name": "roborak/review", "status": "success"}]), t
        ),
    )
    report = GitLabPublisher(target=target, token="tok").publish(make_result())

    assert not report.status_posted, "the commit still carries the old passing verdict"
    assert "400" in (report.status_skipped or "")


def test_a_refused_status_with_no_status_to_read_back_is_reported(monkeypatch):
    """Silence about the state is not evidence the state is right."""
    target = Target("gitlab", "gitlab.com", "acme/web", 298)
    monkeypatch.setattr(
        "roborak.publish.gitlab.ForgeClient",
        lambda t, tok: client_with(refusing_status_handler([]), t),
    )
    report = GitLabPublisher(target=target, token="tok").publish(make_result())

    assert not report.status_posted
    assert report.status_skipped


def test_gitlab_reports_a_rejected_status_that_is_not_an_unchanged_one(monkeypatch):
    """400 and 409 also cover a bad request and a concurrent update."""
    target = Target("gitlab", "gitlab.com", "acme/web", 298)

    def handler(request: httpx.Request) -> httpx.Response:
        if "/statuses/" in request.url.path:
            return httpx.Response(409, text="Conflict: the status was updated concurrently")
        return httpx.Response(201, json={"id": "1"})

    monkeypatch.setattr(
        "roborak.publish.gitlab.ForgeClient", lambda t, tok: client_with(handler, t)
    )
    report = GitLabPublisher(target=target, token="tok").publish(make_result())

    assert not report.status_posted
    assert "409" in (report.status_skipped or "")


def test_a_change_with_no_head_commit_skips_the_status(monkeypatch):
    posted: list[tuple[str, dict]] = []
    target = Target("github", "github.com", "acme/web", 42)
    monkeypatch.setattr(
        "roborak.publish.github.ForgeClient",
        lambda t, tok: client_with(recording_handler(posted), t),
    )
    result = make_result()
    assert result.changeset is not None and result.changeset.forge_ref is not None
    result.changeset.forge_ref.head_sha = None
    result.changeset.head_sha = ""

    report = GitHubPublisher(target=target, token="tok").publish(result)

    assert status_calls(posted) == []
    assert report.status_skipped == "no head commit sha for this change"
    assert len(report.posted) == 1, "the review is not lost with the status"


def test_the_status_links_back_to_the_change(monkeypatch):
    posted: list[tuple[str, dict]] = []
    target = Target("github", "github.com", "acme/web", 42)
    monkeypatch.setattr(
        "roborak.publish.github.ForgeClient",
        lambda t, tok: client_with(recording_handler(posted), t),
    )
    result = make_result()
    assert result.changeset is not None and result.changeset.forge_ref is not None
    result.changeset.forge_ref.web_url = "https://github.com/acme/web/pull/42"

    GitHubPublisher(target=target, token="tok").publish(result)

    assert status_calls(posted)[0][1]["target_url"] == "https://github.com/acme/web/pull/42"


REVIEW_URL = "https://github.com/acme/web/pull/42#pullrequestreview-1"


def test_the_status_links_to_the_summary_comment(monkeypatch):
    """The verdict is in the summary; the status points at it, not at the page."""
    posted: list[tuple[str, dict]] = []
    target = Target("github", "github.com", "acme/web", 42)

    def handler(request: httpx.Request) -> httpx.Response:
        posted.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"id": 1, "html_url": REVIEW_URL})

    monkeypatch.setattr(
        "roborak.publish.github.ForgeClient", lambda t, tok: client_with(handler, t)
    )
    result = make_result()
    assert result.changeset is not None and result.changeset.forge_ref is not None
    result.changeset.forge_ref.web_url = "https://github.com/acme/web/pull/42"

    GitHubPublisher(target=target, token="tok").publish(result)

    assert status_calls(posted)[0][1]["target_url"] == REVIEW_URL


def test_a_gitlab_status_links_to_the_note_it_just_wrote(monkeypatch):
    posted: list[tuple[str, dict]] = []
    target = Target("gitlab", "gitlab.com", "acme/web", 298)

    def handler(request: httpx.Request) -> httpx.Response:
        posted.append((request.url.path, json.loads(request.content)))
        return httpx.Response(201, json={"id": 77})

    monkeypatch.setattr(
        "roborak.publish.gitlab.ForgeClient", lambda t, tok: client_with(handler, t)
    )
    result = make_result()
    assert result.changeset is not None and result.changeset.forge_ref is not None
    result.changeset.forge_ref.web_url = "https://gitlab.com/acme/web/-/merge_requests/298"

    GitLabPublisher(target=target, token="tok").publish(result)

    assert status_calls(posted)[0][1]["target_url"].endswith("/merge_requests/298#note_77")
