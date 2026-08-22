"""Issue context and the change it points at.

Two things are worth guarding here. First, that an issue is read faithfully and
kept small -- it rides in the prompt's scaffolding headroom, which the chunker
does not measure, so an unbounded issue would silently squeeze out the diff.
Second, that link resolution never guesses: a cross-project merge request or a
cross-reference from another repository is not the change under review.
"""

from __future__ import annotations

import httpx
import pytest

from roborak.core.config import ForgeConfig
from roborak.core.models import Issue
from roborak.sources.base import SourceError
from roborak.sources.forge import (
    Target,
    detect_provider,
    parse_target,
    provider_from_url,
)
from roborak.sources.issue import (
    MAX_COMMENT_CHARS,
    MAX_ISSUE_COMMENTS,
    GitHubIssueSource,
    GitLabIssueSource,
    resolve_linked_change,
)

GITLAB_TARGET = Target("gitlab", "gitlab.com", "acme/web", 42)
GITHUB_TARGET = Target("github", "github.com", "acme/web", 42)


def routed(source_cls, target: Target, routes: dict[str, object], monkeypatch):
    """Build a source whose ForgeClient answers from a path -> payload table."""

    def handler(request: httpx.Request) -> httpx.Response:
        for suffix, payload in routes.items():
            if request.url.path.endswith(suffix):
                return httpx.Response(200, json=payload)
        return httpx.Response(404, json={"message": "no route"})

    def factory(target_: Target, token: str, timeout: float = 30.0):
        from roborak.sources.forge import ForgeClient

        client = ForgeClient(target_, token)
        client._client = httpx.Client(
            base_url=target_.api_base, transport=httpx.MockTransport(handler)
        )
        return client

    monkeypatch.setattr("roborak.sources.issue.ForgeClient", factory)
    return source_cls(target=target, token="tok")


@pytest.mark.parametrize(
    ("url", "provider", "expected"),
    [
        (
            "https://gitlab.com/acme/web/-/issues/42",
            "gitlab",
            ("gitlab.com", "acme/web", 42),
        ),
        (
            "https://gitlab.example.com/group/sub/app/-/issues/7",
            "gitlab",
            ("gitlab.example.com", "group/sub/app", 7),
        ),
        ("https://github.com/acme/web/issues/42", "github", ("github.com", "acme/web", 42)),
    ],
)
def test_issue_url_parsing(url, provider, expected):
    target = parse_target(url, provider, kind="issue")
    assert (target.host, target.project, target.number) == expected


def test_change_parsing_still_rejects_an_issue_url():
    with pytest.raises(SourceError):
        parse_target("https://github.com/acme/web/issues/42", "github")
    with pytest.raises(SourceError):
        parse_target("https://gitlab.com/acme/web/-/issues/42", "gitlab")


def test_issue_parsing_rejects_a_change_url():
    with pytest.raises(SourceError):
        parse_target("https://github.com/acme/web/pull/42", "github", kind="issue")


def test_bare_number_needs_a_project():
    target = parse_target("42", "github", host="github.com", project="acme/web", kind="issue")
    assert (target.host, target.project, target.number) == ("github.com", "acme/web", 42)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://gitlab.com/acme/web/-/issues/42", "gitlab"),
        ("https://gitlab.com/acme/web/-/merge_requests/42", "gitlab"),
        ("https://github.com/acme/web/issues/42", "github"),
        ("https://github.com/acme/web/pull/42", "github"),
        ("42", None),
        ("https://example.com/whatever", None),
    ],
)
def test_provider_from_url(url, expected):
    assert provider_from_url(url) == expected


@pytest.mark.parametrize(
    ("remote_host", "expected"),
    [
        ("gitlab.com", "gitlab"),
        ("gitlab.corp.example", "gitlab"),
        ("github.com", "github"),
        ("git.corp.example", None),
        (None, None),
    ],
)
def test_detect_provider(remote_host, expected, monkeypatch):
    monkeypatch.setattr("roborak.sources.forge.detect_host", lambda *a, **k: remote_host)
    assert detect_provider() == expected


def test_a_configured_host_names_the_forge_of_an_ambiguous_domain(monkeypatch):
    monkeypatch.setattr("roborak.sources.forge.detect_host", lambda *a, **k: "git.corp.example")
    forge = ForgeConfig(hosts={"gitlab": "https://git.corp.example/"})
    assert detect_provider(forge=forge) == "gitlab"
    assert detect_provider(forge=ForgeConfig(hosts={"gitlab": "gl.other"})) is None


GITLAB_ISSUE = {
    "title": "Sessions can be hijacked",
    "description": "Tokens are compared with ==, which leaks timing.",
    "labels": ["security", "bug"],
    "state": "opened",
    "web_url": "https://gitlab.com/acme/web/-/issues/42",
}


def test_gitlab_issue_is_read_and_system_notes_dropped(monkeypatch):
    notes = [
        {"body": "Use hmac.compare_digest.", "system": False},
        {"body": "changed the description", "system": True},
        {"body": "Also rate-limit the endpoint.", "system": False},
    ]
    source = routed(
        GitLabIssueSource,
        GITLAB_TARGET,
        {"/issues/42/notes": notes, "/issues/42": GITLAB_ISSUE},
        monkeypatch,
    )
    issue = source.load()

    assert issue.title == "Sessions can be hijacked"
    assert issue.body.startswith("Tokens are compared")
    assert issue.labels == ["security", "bug"]
    assert issue.state == "opened"
    assert issue.reference == "#42"
    assert issue.comments == ["Use hmac.compare_digest.", "Also rate-limit the endpoint."]


def test_issue_comments_are_capped(monkeypatch):
    notes = [{"body": f"comment {i}", "system": False} for i in range(MAX_ISSUE_COMMENTS + 10)]
    source = routed(
        GitLabIssueSource,
        GITLAB_TARGET,
        {"/issues/42/notes": notes, "/issues/42": GITLAB_ISSUE},
        monkeypatch,
    )
    assert len(source.load().comments) == MAX_ISSUE_COMMENTS


def test_a_long_comment_is_truncated(monkeypatch):
    notes = [{"body": "x" * (MAX_COMMENT_CHARS * 3), "system": False}]
    source = routed(
        GitLabIssueSource,
        GITLAB_TARGET,
        {"/issues/42/notes": notes, "/issues/42": GITLAB_ISSUE},
        monkeypatch,
    )
    (comment,) = source.load().comments
    assert len(comment) <= MAX_COMMENT_CHARS + len(" […]")
    assert comment.endswith("[…]")


def test_gitlab_linked_merge_request_prefers_the_open_one(monkeypatch):
    related = [
        {
            "iid": 10,
            "state": "merged",
            "updated_at": "2026-08-19T10:00:00Z",
            "references": {"full": "acme/web!10"},
        },
        {
            "iid": 11,
            "state": "opened",
            "updated_at": "2026-08-18T10:00:00Z",
            "references": {"full": "acme/web!11"},
        },
    ]
    source = routed(
        GitLabIssueSource,
        GITLAB_TARGET,
        {"/related_merge_requests": related},
        monkeypatch,
    )
    linked = source.linked_changes()
    assert max(linked, key=lambda c: (c.is_open, c.updated_at)).number == 11


def test_gitlab_ignores_a_merge_request_in_another_project(monkeypatch):
    related = [
        {"iid": 5, "state": "opened", "references": {"full": "other/repo!5"}},
        {"iid": 6, "state": "opened", "references": {"full": "acme/web!6"}},
    ]
    source = routed(
        GitLabIssueSource,
        GITLAB_TARGET,
        {"/related_merge_requests": related},
        monkeypatch,
    )
    assert [c.number for c in source.linked_changes()] == [6]


GITHUB_ISSUE = {
    "title": "Sessions can be hijacked",
    "body": "Tokens are compared with ==, which leaks timing.",
    "labels": [{"name": "security"}, {"name": "bug"}],
    "state": "open",
    "html_url": "https://github.com/acme/web/issues/42",
}


def test_github_issue_flattens_label_objects(monkeypatch):
    source = routed(
        GitHubIssueSource,
        GITHUB_TARGET,
        {"/issues/42/comments": [{"body": "Use hmac."}], "/issues/42": GITHUB_ISSUE},
        monkeypatch,
    )
    issue = source.load()
    assert issue.labels == ["security", "bug"]
    assert issue.comments == ["Use hmac."]
    assert issue.web_url == "https://github.com/acme/web/issues/42"


def test_a_pull_request_served_from_the_issues_endpoint_is_refused(monkeypatch):
    payload = {**GITHUB_ISSUE, "pull_request": {"url": "https://api.github.com/…"}}
    source = routed(
        GitHubIssueSource,
        GITHUB_TARGET,
        {"/issues/42/comments": [], "/issues/42": payload},
        monkeypatch,
    )
    with pytest.raises(SourceError, match="pull request, not an issue"):
        source.load()


def _cross_reference(number: int, *, repo: str = "acme/web", is_pr: bool = True, state="open"):
    issue: dict[str, object] = {
        "number": number,
        "state": state,
        "updated_at": f"2026-08-{number:02d}T00:00:00Z",
        "repository": {"full_name": repo},
    }
    if is_pr:
        issue["pull_request"] = {"url": "https://api.github.com/…"}
    return {"event": "cross-referenced", "source": {"issue": issue}}


def test_github_timeline_yields_the_linked_pull_request(monkeypatch):
    timeline = [
        {"event": "labeled"},
        _cross_reference(11, is_pr=False),
        _cross_reference(12, repo="someone/fork"),
        _cross_reference(13),
    ]
    source = routed(GitHubIssueSource, GITHUB_TARGET, {"/timeline": timeline}, monkeypatch)
    assert [c.number for c in source.linked_changes()] == [13]


def test_no_linked_change_is_not_an_error(monkeypatch):
    source = routed(GitHubIssueSource, GITHUB_TARGET, {"/timeline": []}, monkeypatch)
    assert source.linked_changes() == []


def test_resolve_linked_change_returns_none_when_nothing_links(monkeypatch):
    monkeypatch.setattr("roborak.sources.issue.GitHubIssueSource.linked_changes", lambda self: [])
    assert resolve_linked_change(GITHUB_TARGET, "tok") is None


def test_resolve_linked_change_picks_open_over_recent(monkeypatch):
    from roborak.sources.issue import LinkedChange

    monkeypatch.setattr(
        "roborak.sources.issue.GitHubIssueSource.linked_changes",
        lambda self: [
            LinkedChange(number=1, state="closed", updated_at="2026-08-20T00:00:00Z"),
            LinkedChange(number=2, state="open", updated_at="2026-08-01T00:00:00Z"),
        ],
    )
    linked = resolve_linked_change(GITHUB_TARGET, "tok")
    assert linked is not None and linked.number == 2


def test_issue_reference_is_hash_on_both_forges():
    for provider in ("gitlab", "github"):
        issue = Issue(provider=provider, host="h", project="a/b", number=7)
        assert issue.reference == "#7"
