"""The trust boundary every roborak subprocess crosses.

Two stages run repository-controlled commands: static analysis, which loads a
project's linters, their plugins and their configuration, and verification, which
runs the project's own test commands. Both face the same question -- whether the
checkout in front of us is one whose code we are willing to execute -- and both
must answer it the same way, so the answer lives here rather than in each of them.

The posture, in three rules:

- outside CI the checkout is the user's own working tree, and running it directly
  is what they asked for;
- inside CI it is whatever a contributor pushed, so it runs read-only, without a
  network, or it does not run at all;
- either way the command is handed runtime plumbing and never the caller's
  credentials.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

_SAFE_ENV = {
    "PATH",
    "PATHEXT",
    "SystemRoot",
    "LANG",
    "LANGUAGE",
    "LC_ALL",
    "LC_CTYPE",
    "TERM",
    "VIRTUAL_ENV",
}


def safe_environment(tmpdir: str | None = None) -> dict[str, str]:
    """Runtime plumbing, never the caller's credentials.

    ``tmpdir`` is the scratch directory the command is pointed at. It defaults to
    the platform's own -- Windows has no ``/tmp`` -- but the sandbox passes
    ``/tmp`` explicitly, because that is what ``--tmpfs /tmp`` puts inside it.
    """
    scratch = tmpdir or tempfile.gettempdir()
    env = {key: value for key, value in os.environ.items() if key in _SAFE_ENV}
    env.update(
        {
            "HOME": scratch,
            "TMPDIR": scratch,
            "XDG_CACHE_HOME": str(Path(scratch) / "roborak-static-cache"),
        }
    )
    return env


def sandbox_prefix(repo: Path) -> list[str] | None:
    """A bubblewrap prefix for read-only, networkless execution, or ``None``.

    ``None`` means bubblewrap is not installed, which is never a licence to run
    the command anyway: the caller skips the stage instead.
    """
    bwrap = shutil.which("bwrap")
    if bwrap is None:
        return None
    repo_parents = [
        item
        for parent in reversed(repo.resolve().parents)
        if parent != Path("/")
        for item in ("--dir", str(parent))
    ]
    return [
        bwrap,
        "--die-with-parent",
        "--unshare-net",
        "--ro-bind",
        "/usr",
        "/usr",
        "--ro-bind-try",
        "/bin",
        "/bin",
        "--ro-bind-try",
        "/lib",
        "/lib",
        "--ro-bind-try",
        "/lib64",
        "/lib64",
        "--ro-bind",
        "/etc",
        "/etc",
        *repo_parents,
        "--ro-bind",
        str(repo),
        str(repo),
        "--tmpfs",
        "/tmp",
        # A private device filesystem rather than the host's: the command needs
        # the standard nodes -- null, zero, urandom, a pts pair, shm -- and has no
        # business reaching the disks, the terminals or anything else in /dev.
        "--dev",
        "/dev",
        # A PID namespace of its own, so the procfs mounted below lists the
        # sandboxed process and nothing else. Without it `--proc` still shows
        # every process on the host -- their command lines, their environments --
        # to a command we decided we did not trust. It also makes the namespace
        # the kill boundary: when the runner's timeout kills bwrap, everything the
        # suite spawned dies with it rather than being orphaned onto the machine.
        "--unshare-pid",
        "--proc",
        "/proc",
        # Its own session, so it cannot push characters back onto the terminal
        # that started the review with TIOCSTI. Output is captured through pipes,
        # so nothing here wanted a controlling terminal anyway.
        "--new-session",
        "--chdir",
        str(repo),
        "--",
    ]


def in_ci() -> bool:
    """Whether ``CI`` is set to anything that is not an explicit denial."""
    value = os.getenv("CI", "").strip().lower()
    return value not in {"", "0", "false", "no"}
