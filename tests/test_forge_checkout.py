"""Fetching a throwaway checkout of a merge or pull request nobody has locally.

The claims worth defending here are the ones that make the feature safe rather
than the one that makes it useful: that the tree searched is the reviewed commit
and not merely whatever the fetch produced, that a failure is a note rather than
an exception, and that the directory is gone afterwards either way.

The fetch tests drive real git against a real repository over a ``file://``
remote. Stubbing it would leave the interesting half untested -- what git
actually does with a shallow fetch of a sha a server may refuse to serve. Only
``_source`` is exercised separately, because deciding *which* URL to fetch from
needs the https and ssh remotes a local fixture cannot also be reachable at.
"""

from __future__ import annotations

import os
import stat
import subprocess
import tempfile
import textwrap
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from roborak.context import forge_checkout, impact
from roborak.context.diff import whole_file_hunk
from roborak.core.config import ForgeCheckout, ImpactConfig
from roborak.core.models import ChangedFile, ChangeSet, ForgeRef, ImpactStatus

SERVICE = """\
    def charge_card(amount):
        return amount
"""

CALLER = """\
    def pay():
        return charge_card(1)
"""


def git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ("git", *args), cwd=repo, check=True, capture_output=True, text=True, encoding="utf-8"
    )
    return done.stdout


@pytest.fixture
def forge(tmp_path: Path) -> Path:
    """The "remote": a real repository holding a caller of the changed symbol.

    It publishes the change under ``refs/pull/7/head`` and
    ``refs/merge-requests/7/head`` the way the two forges do, so the fallback for
    servers that refuse to serve a bare sha is exercised rather than assumed.
    """
    origin = tmp_path / "forge"
    origin.mkdir()
    git(origin, "init", "-q")
    git(origin, "config", "user.email", "t@example.com")
    git(origin, "config", "user.name", "t")
    (origin / "service.py").write_text(textwrap.dedent(SERVICE), encoding="utf-8")
    (origin / "checkout.py").write_text(textwrap.dedent(CALLER), encoding="utf-8")
    git(origin, "add", "-A")
    git(origin, "commit", "-qm", "seed")
    head = git(origin, "rev-parse", "HEAD").strip()
    git(origin, "update-ref", "refs/pull/7/head", head)
    git(origin, "update-ref", "refs/merge-requests/7/head", head)
    return origin


@pytest.fixture
def local(tmp_path: Path) -> Path:
    """The user's checkout: never fetched, so it does not hold the reviewed commit.

    That is the situation the feature exists for -- reviewing a branch this
    machine has never seen.
    """
    repo = tmp_path / "local"
    repo.mkdir()
    git(repo, "init", "-q")
    return repo


@pytest.fixture
def fetch_from(monkeypatch: pytest.MonkeyPatch) -> Callable[[Path | str], None]:
    """Point the fetch at a given path, leaving everything downstream real.

    ``_source`` is replaced rather than the fetch itself: a ``file://`` remote can
    never satisfy the project and host checks that ``_source`` exists to make, and
    faking those checks here would test the fixture. They get their own tests
    below, against the URL shapes they actually see.
    """

    def use(path: Path | str) -> None:
        monkeypatch.setattr(forge_checkout, "_source", lambda *a, **k: (f"file://{path}", True))

    return use


def head_of(repo: Path) -> str:
    return git(repo, "rev-parse", "HEAD").strip()


def forge_change(head: str, *, provider: str = "github") -> ChangeSet:
    """A change as a forge source builds one: hunks only, no file content.

    ``new_content`` is left ``None`` on purpose. Neither forge source populates
    it, so a fixture that filled it in would test a shape production never emits.
    """
    content = textwrap.dedent(SERVICE)
    file = ChangedFile(
        path="service.py",
        change_type="added",
        language="python",
        hunks=whole_file_hunk(content),
    )
    return ChangeSet(
        files=[file],
        origin=provider,  # type: ignore[arg-type]
        head_sha=head,
        forge_ref=ForgeRef(
            provider=provider,  # type: ignore[arg-type]
            host="example.com",
            project="team/project",
            number=7,
            head_sha=head,
        ),
    )


@pytest.fixture
def watch_scratch(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Every temporary directory the module creates, so cleanup can be asserted."""
    made: list[Path] = []
    real = tempfile.mkdtemp

    def record(suffix: str | None = None, prefix: str | None = None, dir: str | None = None) -> str:
        path = real(suffix, prefix, dir)
        made.append(Path(path))
        return path

    monkeypatch.setattr(forge_checkout.tempfile, "mkdtemp", record)
    return made


# --- the feature itself ------------------------------------------------------


@pytest.mark.parametrize("provider", ["github", "gitlab"])
def test_a_forge_change_this_repo_lacks_is_fetched_and_searched(
    local: Path, forge: Path, fetch_from: Callable[[Path | str], None], provider: str
) -> None:
    """The whole point: consumers found for a commit the user never fetched."""
    fetch_from(forge)

    result = impact.analyse(forge_change(head_of(forge), provider=provider), local, ImpactConfig())

    assert result.status is not ImpactStatus.UNAVAILABLE
    node = next(node for node in result.nodes if node.name == "charge_card")
    assert [consumer.path for consumer in node.consumers] == ["checkout.py"]


def test_the_report_says_the_search_used_a_temporary_checkout(
    local: Path, forge: Path, fetch_from: Callable[[Path | str], None]
) -> None:
    fetch_from(forge)
    head = head_of(forge)

    result = impact.analyse(forge_change(head), local, ImpactConfig())

    assert f"temporary checkout of {head[:12]}" in result.notes[0]
    assert "may not hold exactly the code under review" not in " ".join(result.notes)


def test_a_verified_temporary_checkout_is_not_reported_as_limited(
    local: Path, forge: Path, fetch_from: Callable[[Path | str], None]
) -> None:
    """The tree *is* the reviewed commit, so the local-checkout caveat is false.

    ``limited`` exists to say the searched tree may not be the reviewed one. A
    checkout verified against ``head_sha`` is exactly that tree, and flattening it
    to ``limited`` anyway would understate what the map knows.
    """
    fetch_from(forge)

    result = impact.analyse(forge_change(head_of(forge)), local, ImpactConfig())

    assert result.status is not ImpactStatus.LIMITED


def test_content_is_read_from_the_tree_without_touching_the_changeset(
    local: Path, forge: Path, fetch_from: Callable[[Path | str], None]
) -> None:
    """Forge sources send hunks, not files; the tree supplies the rest.

    And the changeset must come back untouched: content read here is evidence for
    the map, and writing it back would turn whole files into diff surface.
    """
    fetch_from(forge)
    changeset = forge_change(head_of(forge))
    assert changeset.files[0].new_content is None

    result = impact.analyse(changeset, local, ImpactConfig())

    assert result.nodes
    assert changeset.files[0].new_content is None


def test_a_server_that_refuses_a_bare_sha_falls_back_to_the_published_ref(
    local: Path, forge: Path, fetch_from: Callable[[Path | str], None]
) -> None:
    """``uploadpack.allowReachableSHA1InWant`` is off on plenty of instances."""
    git(forge, "config", "uploadpack.allowReachableSHA1InWant", "false")
    git(forge, "config", "uploadpack.allowAnySHA1InWant", "false")
    fetch_from(forge)

    result = impact.analyse(forge_change(head_of(forge)), local, ImpactConfig())

    assert result.nodes


# --- staying out of the way --------------------------------------------------


def test_off_leaves_the_change_unavailable_and_fetches_nothing(
    local: Path, forge: Path, watch_scratch: list[Path]
) -> None:
    config = ImpactConfig(forge_checkout=ForgeCheckout.OFF)

    result = impact.analyse(forge_change(head_of(forge)), local, config)

    assert result.status is ImpactStatus.UNAVAILABLE
    assert "no checkout to search" in result.notes[0]
    assert watch_scratch == []


def test_a_head_already_present_locally_is_searched_where_it_sits(
    local: Path, forge: Path, watch_scratch: list[Path]
) -> None:
    """Nothing is fetched when the commit is already here, and it stays limited."""
    git(local, "fetch", "-q", "--depth=1", f"file://{forge}", "HEAD")
    git(local, "checkout", "-q", "FETCH_HEAD")

    result = impact.analyse(forge_change(head_of(forge)), local, ImpactConfig())

    assert watch_scratch == []
    assert result.status is ImpactStatus.LIMITED
    assert "may not hold exactly the code under review" in result.notes[0]


# --- degradation -------------------------------------------------------------


def test_an_unreachable_remote_degrades_to_a_note(
    tmp_path: Path,
    local: Path,
    forge: Path,
    fetch_from: Callable[[Path | str], None],
    watch_scratch: list[Path],
) -> None:
    fetch_from(tmp_path / "nowhere")

    result = impact.analyse(forge_change(head_of(forge)), local, ImpactConfig())

    assert result.status is ImpactStatus.UNAVAILABLE
    assert any("could not be fetched from the forge" in note for note in result.notes)
    assert watch_scratch and not watch_scratch[0].exists()


def test_a_tree_that_is_not_the_reviewed_commit_is_refused(
    local: Path, forge: Path, fetch_from: Callable[[Path | str], None], watch_scratch: list[Path]
) -> None:
    """``refs/pull/N/head`` moves. Searching whatever it points at now would be a lie.

    The sha fetch fails because the forge has never heard of it, the published ref
    then fetches perfectly well, and the verification is the only thing standing
    between the search and a tree nobody asked about.
    """
    fetch_from(forge)
    moved = forge_change("0" * 40)

    result = impact.analyse(moved, local, ImpactConfig())

    assert result.status is ImpactStatus.UNAVAILABLE
    assert any("not 000000000000" in note for note in result.notes)
    assert watch_scratch and not watch_scratch[0].exists()


def test_the_scratch_directory_is_removed_after_a_successful_review(
    local: Path, forge: Path, fetch_from: Callable[[Path | str], None], watch_scratch: list[Path]
) -> None:
    fetch_from(forge)

    result = impact.analyse(forge_change(head_of(forge)), local, ImpactConfig())

    assert result.nodes
    assert watch_scratch and not watch_scratch[0].exists()


def test_a_review_whose_fetch_fails_still_reports_why(
    local: Path, forge: Path, watch_scratch: list[Path]
) -> None:
    """Both halves of the answer: no local checkout, and no fetched one either."""
    result = impact.analyse(forge_change(head_of(forge)), local, ImpactConfig())

    assert result.status is ImpactStatus.UNAVAILABLE
    assert "no checkout to search" in result.notes[0]
    assert "A temporary checkout of the change was attempted" in result.notes[1]


# --- where to fetch from -----------------------------------------------------


def test_the_local_remote_is_used_when_it_names_the_same_project(local: Path) -> None:
    """The good path: whatever the user already authenticates with.

    A credential helper or an SSH agent keeps working and no token is handled at
    all, which is why this is preferred over a URL built from the change.
    """
    git(local, "remote", "add", "origin", "https://example.com/team/project.git")

    assert forge_checkout._source(forge_change("a" * 40), local) == (
        "https://example.com/team/project.git",
        True,
    )


def test_an_ssh_remote_for_the_same_project_is_used_too(local: Path) -> None:
    git(local, "remote", "add", "origin", "git@example.com:team/project.git")

    source = forge_checkout._source(forge_change("a" * 40), local)

    assert source == ("git@example.com:team/project.git", True)


def test_a_remote_naming_another_project_is_not_trusted(local: Path) -> None:
    """Otherwise an unrelated ``origin`` would silently redirect the fetch."""
    git(local, "remote", "add", "origin", "https://example.com/someone/else.git")

    source = forge_checkout._source(forge_change("a" * 40), local)

    assert source == ("https://example.com/team/project.git", False)


def test_a_numeric_gitlab_project_has_no_clone_url_to_build(local: Path) -> None:
    """A project id addresses the API, not a clone path."""
    changeset = forge_change("a" * 40, provider="gitlab")
    assert changeset.forge_ref is not None
    changeset.forge_ref.project = "4815162342"

    assert forge_checkout._source(changeset, local) is None


def test_a_change_with_no_forge_reference_has_nowhere_to_fetch_from(local: Path) -> None:
    changeset = forge_change("a" * 40)
    changeset.forge_ref = None

    assert forge_checkout._source(changeset, local) is None


# --- cleanup -----------------------------------------------------------------


def test_a_checkout_git_marked_read_only_is_still_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Windows makes everything under ``.git/objects`` read-only.

    Deleting one raises ``PermissionError`` there, and swallowing that would leave
    a full clone in the user's temp directory after every review -- the failure
    cleanup exists to prevent, made invisible.

    POSIX unlinks by directory permission, so the read-only bit alone would not
    reach the recovery path on the two thirds of CI that is not Windows. Removal
    of the read-only object is refused here instead, exactly as Windows refuses
    it, so the retry is what the assertion rests on everywhere.
    """
    scratch = tmp_path / "scratch"
    (scratch / ".git" / "objects").mkdir(parents=True)
    obj = scratch / ".git" / "objects" / "cafe"
    obj.write_text("packed", encoding="utf-8")
    obj.chmod(stat.S_IREAD)

    unlink = os.unlink

    def refuse_while_read_only(path: str, **kwargs: object) -> None:
        if Path(path) == obj and not stat.S_IMODE(os.stat(path).st_mode) & stat.S_IWRITE:
            raise PermissionError(13, "Access is denied", str(path))
        unlink(path)

    monkeypatch.setattr(os, "unlink", refuse_while_read_only)

    forge_checkout._remove(scratch)

    assert not scratch.exists()


def test_a_checkout_that_cannot_be_removed_does_not_fail_the_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A directory roborak was merely borrowing must not take a review down."""
    scratch = tmp_path / "stuck"
    scratch.mkdir()

    def refuse(*args: object, **kwargs: object) -> None:
        raise OSError("in use")

    monkeypatch.setattr(forge_checkout.shutil, "rmtree", refuse)

    forge_checkout._remove(scratch)  # must not raise


# --- credentials -------------------------------------------------------------


def test_git_is_never_left_waiting_on_a_prompt() -> None:
    """A review runs behind a spinner: a password prompt is a hang, not a question."""
    env = forge_checkout._environment("https://example.com/a/b.git", token=None)

    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GIT_ASKPASS"] == ""
    assert "BatchMode=yes" in env["GIT_SSH_COMMAND"]


def test_a_token_travels_in_the_environment_and_never_in_argv() -> None:
    """``ps`` is world-readable; a token on the command line would outlive the run."""
    env = forge_checkout._environment("https://example.com/a/b.git", token="secret")

    assert env["GIT_CONFIG_KEY_0"] == "http.extraheader"
    assert env["GIT_CONFIG_VALUE_0"] == "Authorization: Bearer secret"


def test_a_token_is_not_attached_to_a_remote_that_is_not_https() -> None:
    env = forge_checkout._environment("git@example.com:a/b.git", token="secret")

    assert "GIT_CONFIG_VALUE_0" not in env


def test_an_inherited_ssh_command_keeps_its_options_and_gains_batch_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``GIT_TERMINAL_PROMPT`` says nothing to ssh, and a CI runner sets this."""
    monkeypatch.setenv("GIT_SSH_COMMAND", "ssh -i /tmp/key")

    env = forge_checkout._environment("git@example.com:a/b.git", token=None)

    assert "-i /tmp/key" in env["GIT_SSH_COMMAND"]
    assert "BatchMode=yes" in env["GIT_SSH_COMMAND"]


def test_an_explicit_batch_mode_choice_is_left_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    """ssh honours the first occurrence of an option: appending would not win."""
    monkeypatch.setenv("GIT_SSH_COMMAND", "ssh -oBatchMode=no")

    env = forge_checkout._environment("git@example.com:a/b.git", token=None)

    assert env["GIT_SSH_COMMAND"] == "ssh -oBatchMode=no"


# --- the wall clock ----------------------------------------------------------


class FakeClock:
    """``time`` as ``forge_checkout`` sees it, jumping once the setup is done.

    The module's own reference is replaced rather than ``time.monotonic`` itself,
    which ``subprocess`` uses to enforce the timeouts this is measuring.
    """

    def __init__(self, hold: int, then: float) -> None:
        self.hold = hold
        self.then = then
        self.calls = 0

    def monotonic(self) -> float:
        self.calls += 1
        return 0.0 if self.calls <= self.hold else self.then


@pytest.fixture
def record_git(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, ...]]:
    """Every git command the module actually spawns, in order."""
    seen: list[tuple[str, ...]] = []
    real = subprocess.run

    def run(args, *rest, **kwargs):  # type: ignore[no-untyped-def]
        seen.append(tuple(args))
        return real(args, *rest, **kwargs)

    monkeypatch.setattr(forge_checkout.subprocess, "run", run)
    return seen


def test_the_timeout_is_one_wall_clock_for_the_whole_attempt(
    local: Path,
    forge: Path,
    fetch_from: Callable[[Path | str], None],
    record_git: list[tuple[str, ...]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Six commands each allowed the full timeout would cost minutes, not seconds."""
    fetch_from(forge)
    # Held for the deadline itself, `init` and `remote add`; spent by the fetch.
    monkeypatch.setattr(forge_checkout, "time", FakeClock(hold=3, then=100.0))

    result = impact.analyse(
        forge_change(head_of(forge)), local, ImpactConfig(forge_checkout_timeout_seconds=1)
    )

    assert not any("fetch" in args for args in record_git)
    assert any("could not be fetched from the forge" in note for note in result.notes)


def test_a_command_with_no_time_left_is_not_spawned(
    tmp_path: Path, record_git: list[tuple[str, ...]]
) -> None:
    assert forge_checkout._run(tmp_path, "init", env={}, deadline=time.monotonic() - 1) is None
    assert record_git == []


# --- falling back to the token -----------------------------------------------


def test_a_local_remote_that_cannot_authenticate_is_retried_with_the_token(
    tmp_path: Path,
    local: Path,
    forge: Path,
    monkeypatch: pytest.MonkeyPatch,
    record_git: list[tuple[str, ...]],
) -> None:
    """A private repo with no credential helper: the token roborak holds is the answer."""
    monkeypatch.setattr(
        forge_checkout, "_source", lambda *a, **k: (f"file://{tmp_path / 'nowhere'}", True)
    )
    retry = dict(os.environ) | {"ROBORAK_RETRY": "1"}
    monkeypatch.setattr(forge_checkout, "_authenticated", lambda *a, **k: retry)
    envs: list[str] = []
    real = forge_checkout._run

    def spy(scratch, *args, env, deadline):  # type: ignore[no-untyped-def]
        if args[0] == "fetch":
            envs.append(env.get("ROBORAK_RETRY", ""))
        return real(scratch, *args, env=env, deadline=deadline)

    monkeypatch.setattr(forge_checkout, "_run", spy)

    result = impact.analyse(forge_change(head_of(forge)), local, ImpactConfig())

    # Both refs unauthenticated, then both again carrying the token.
    assert envs == ["", "", "1", "1"]
    assert any("could not be fetched from the forge" in note for note in result.notes)


def test_the_token_goes_to_the_forge_host_and_no_lookalike(local: Path) -> None:
    """``_matches`` asks whether the host appears in the URL, which is no bar for a secret."""
    ref = forge_change("a" * 40).forge_ref
    assert ref is not None

    good = forge_checkout._authenticated("https://example.com/team/project.git", ref, "secret")
    assert good is not None
    assert good["GIT_CONFIG_VALUE_0"] == "Authorization: Bearer secret"

    lookalike = "https://example.com.somewhere-else.net/team/project.git"
    assert forge_checkout._authenticated(lookalike, ref, "secret") is None


def test_no_token_and_no_https_means_no_authenticated_retry(local: Path) -> None:
    ref = forge_change("a" * 40).forge_ref
    assert ref is not None

    assert forge_checkout._authenticated("https://example.com/team/project.git", ref, None) is None
    assert forge_checkout._authenticated("git@example.com:team/project.git", ref, "secret") is None
