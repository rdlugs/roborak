"""A throwaway checkout of the change under review, when the local one is not it.

A merge or pull request arrives as a diff. The blast-radius pass needs a *tree*:
finding the unchanged caller of a changed function means searching code the diff
never mentions. When the reviewed head commit happens to sit in the local object
database, ``impact`` searches the working directory and says so. When it does
not -- reviewing a colleague's branch, or any repository nobody has fetched --
there is nothing to search and the whole stage reports itself unavailable.

This fetches one. A temporary directory, a shallow fetch of exactly the reviewed
commit, and a checkout, deleted when the review is done. Four things keep that
from being a liability:

* **Nothing here touches the user's repository.** Not its object database, not
  its refs, not its worktrees, not its configuration. The local repo is read
  once, for the URL of its ``origin`` remote, and never written.
* **It is exactly the reviewed commit or it is nothing.** The fetch is verified
  against ``head_sha`` afterwards, because ``refs/pull/N/head`` is a moving
  target and a race would otherwise hand the search a tree nobody asked about.
  An unverified checkout is discarded rather than downgraded.
* **It cannot hang.** Git is run with interactive prompting disabled and a wall
  clock, so a private repository fails in seconds instead of blocking a review
  forever on a password prompt nobody can see.
* **Nothing here raises.** Every failure is a note and an empty result, and the
  caller carries on with whatever the local checkout could offer -- which is what
  it would have had anyway.
"""

from __future__ import annotations

import logging
import os
import shutil
import stat
import subprocess
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from roborak.core.config import ForgeCheckout, ImpactConfig
from roborak.core.models import ChangeSet, ForgeRef
from roborak.sources.forge import project_from_remote, remote_url, split_host

log = logging.getLogger(__name__)

FORGE_ORIGINS = frozenset({"github", "gitlab"})

_HEAD_REF = {
    "github": "refs/pull/{number}/head",
    "gitlab": "refs/merge-requests/{number}/head",
}
"""The ref a forge publishes for a change, used when fetching the raw sha is refused.

Fetching a commit by its own name needs ``uploadpack.allowReachableSHA1InWant``,
which plenty of instances leave off. Both forges expose the head under a
predictable ref regardless, so the sha is tried first for its precision and this
is the fallback."""


@dataclass(frozen=True)
class Checkout:
    """What the blast-radius search got, and what to say about it.

    ``repo is None`` is the ordinary answer: nothing was fetched, either because
    nothing needed to be or because the attempt did not work out. The caller
    falls back to the local checkout in both cases, which is what it did before
    this module existed.
    """

    repo: Path | None = None
    notes: list[str] = field(default_factory=list)
    verified: bool = False
    """The tree is the reviewed commit, proven by ``rev-parse`` after the fetch.

    Only ever true alongside a ``repo``. It is what lets the caller drop the
    "may not hold exactly the code under review" caveat, so it is never set
    optimistically."""


@contextmanager
def acquire(
    changeset: ChangeSet,
    repo: Path,
    config: ImpactConfig,
    *,
    head_present: bool,
    token: str | None = None,
) -> Iterator[Checkout]:
    """A checkout of ``changeset``'s head, for as long as the ``with`` block runs.

    ``head_present`` is the caller's answer to "is the reviewed commit already in
    the local object database", asked there rather than here so that the one probe
    deciding whether the local checkout is searchable also decides whether to go
    looking for another one. Two copies of that question would drift.

    Yields an empty ``Checkout`` whenever one is unnecessary, disabled or
    unobtainable. The directory is removed on the way out either way.
    """
    if head_present or not _wanted(changeset, config):
        yield Checkout()
        return

    scratch = tempfile.mkdtemp(prefix="roborak-impact-")
    try:
        yield _fetch(changeset, repo, config, Path(scratch), token=token)
    finally:
        _remove(Path(scratch))


def _remove(scratch: Path) -> None:
    """Delete the temporary checkout, including the parts git made read-only.

    Windows marks everything under ``.git/objects`` read-only, and deleting a
    read-only file there raises ``PermissionError``. ``ignore_errors`` would hide
    that rather than solve it, leaving a full clone in the user's temp directory
    after every review -- the failure this exists to prevent, made invisible. So
    the bit is cleared and the delete retried, and only a directory that resists
    even that is given up on, because a review must not fail over a directory it
    was merely borrowing.
    """

    def clear_readonly(func: Callable[[str], None], path: str, _exc: BaseException) -> None:
        os.chmod(path, stat.S_IWRITE)
        func(path)

    try:
        shutil.rmtree(scratch, onexc=clear_readonly)
    except OSError as exc:
        log.debug("could not remove temporary checkout %s: %s", scratch, exc)


def _wanted(changeset: ChangeSet, config: ImpactConfig) -> bool:
    """Whether fetching is allowed and there is enough to fetch with."""
    return (
        config.forge_checkout is ForgeCheckout.AUTO
        and changeset.origin in FORGE_ORIGINS
        and bool(changeset.head_sha)
    )


def _fetch(
    changeset: ChangeSet,
    repo: Path,
    config: ImpactConfig,
    scratch: Path,
    *,
    token: str | None,
) -> Checkout:
    """Populate ``scratch`` with the reviewed commit, or explain why not."""
    head = changeset.head_sha
    forge_ref = changeset.forge_ref
    source = _source(changeset, repo)
    if source is None:
        return Checkout(notes=[_unavailable("no URL for the forge repository could be worked out")])
    url, from_remote = source

    env = _environment(url, token=None if from_remote else token)
    timeout = float(config.forge_checkout_timeout_seconds)
    if _run(scratch, "init", "-q", env=env, timeout=timeout) is None or (
        _run(scratch, "remote", "add", "origin", url, env=env, timeout=timeout) is None
    ):
        return Checkout(notes=[_unavailable("a temporary repository could not be created")])

    refs = [head]
    if forge_ref is not None and (pattern := _HEAD_REF.get(forge_ref.provider)):
        refs.append(pattern.format(number=forge_ref.number))
    if not any(
        _run(scratch, "fetch", "--depth=1", "--no-tags", "origin", ref, env=env, timeout=timeout)
        is not None
        for ref in refs
    ):
        return Checkout(notes=[_unavailable("the commit could not be fetched from the forge")])

    if _run(scratch, "checkout", "-q", "FETCH_HEAD", env=env, timeout=timeout) is None:
        return Checkout(notes=[_unavailable("the fetched commit could not be checked out")])

    # `refs/pull/N/head` moves when the author pushes. Fetching it and assuming it
    # is still the sha the diff was cut from would quietly search a different
    # change, which is worse than searching nothing.
    got = (_run(scratch, "rev-parse", "HEAD", env=env, timeout=timeout) or "").strip()
    if got != head:
        log.debug("temporary checkout is at %s, not the reviewed %s", got[:12], head[:12])
        return Checkout(
            notes=[_unavailable(f"the fetched tree is at {got[:12] or 'nothing'}, not {head[:12]}")]
        )

    return Checkout(
        repo=scratch,
        verified=True,
        notes=[
            f"The change was searched against a temporary checkout of {head[:12]} fetched "
            f"from the forge, not against the local working directory."
        ],
    )


def _source(changeset: ChangeSet, repo: Path) -> tuple[str, bool] | None:
    """Where to fetch from, and whether it came from the local remote.

    The local ``origin`` is preferred by a distance: it is already whatever this
    user authenticates with, so a credential helper or an SSH agent keeps working
    and no token has to be handled at all. It is used only once it agrees with the
    change about which project this is, so a repository whose remote points
    somewhere else entirely cannot redirect the fetch.
    """
    forge_ref = changeset.forge_ref
    if forge_ref is None:
        return None

    url = remote_url("origin", repo)
    if url and _matches(url, forge_ref):
        return url, True

    project = forge_ref.project
    if not project or project.isdigit():
        # GitLab's ``project`` may be a numeric id, which addresses the API and
        # not a clone path. There is no URL to build from it.
        return None
    scheme, host = split_host(forge_ref.host)
    return f"{scheme}://{host}/{project}.git", False


def _matches(url: str, forge_ref: ForgeRef) -> bool:
    """Whether a git remote points at the project the change came from."""
    project = project_from_remote(url)
    if not project or project.casefold() != forge_ref.project.casefold():
        return False
    _, host = split_host(forge_ref.host)
    return host.casefold() in url.casefold()


def _environment(url: str, *, token: str | None) -> dict[str, str]:
    """The parent environment, minus every way git can stop and ask a human.

    Deliberately not ``sandbox.safe_environment``: that strips ``HOME`` and the
    SSH agent, which is exactly the credential state a fetch from the user's own
    remote depends on. What is removed here is narrower and specific -- the
    prompts. A review runs behind a spinner, and a git process waiting on a
    password nobody can see is indistinguishable from a hang.
    """
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ASKPASS"] = ""
    env["SSH_ASKPASS"] = ""
    env.setdefault("GIT_SSH_COMMAND", "ssh -oBatchMode=yes")

    if token and url.startswith("https://"):
        # Through git's config *environment*, never ``-c``: argv is readable by
        # every process on the host, and a token in `ps` output would cost more
        # than this stage is worth.
        env["GIT_CONFIG_COUNT"] = "1"
        env["GIT_CONFIG_KEY_0"] = "http.extraheader"
        env["GIT_CONFIG_VALUE_0"] = f"Authorization: Bearer {token}"
    return env


def _run(scratch: Path, *args: str, env: dict[str, str], timeout: float) -> str | None:
    """Run a git command in the scratch directory, or ``None`` if it did not work."""
    try:
        done = subprocess.run(
            ("git", *args),
            cwd=scratch,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("git %s failed: %s", args[0], exc)
        return None
    if done.returncode != 0:
        log.debug("git %s exited %s: %s", args[0], done.returncode, done.stderr.strip()[:200])
        return None
    return done.stdout


def _unavailable(reason: str) -> str:
    return (
        f"A temporary checkout of the change was attempted so its consumers could be "
        f"searched, but {reason}."
    )
