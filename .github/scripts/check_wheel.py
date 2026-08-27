"""Prove the built wheel ships AST support to a plain `pip install roborak`.

The parser used to sit behind an `ast` extra, so a default installation ran
without one and quietly lost enclosing-symbol context and symbol-seeded blast
radius. Two things have to hold for that not to come back, and neither is visible
in the source tree:

* the wheel's own metadata requires tree-sitter *unconditionally* -- a
  ``; extra == "ast"`` marker creeping back would still build and still test green;
* installing that wheel, naming no extras, actually yields a working parser.

Stdlib only, and no shell globs or ``bin/python`` paths, because CI runs this on
Windows as well as Linux and macOS.
"""

from __future__ import annotations

import re
import subprocess
import sys
import zipfile
from email.parser import BytesParser
from pathlib import Path

REQUIRED = ("tree-sitter", "tree-sitter-language-pack")

NAME = re.compile(r"[A-Za-z0-9._-]+")
"""The distribution name at the head of a requirement, before any version or extra."""

CLEAN_INSTALL_CHECK = (
    "from roborak.context import ast_context; "
    "raise SystemExit(0 if ast_context.available() else 'no parser after a clean install')"
)


def fail(message: str) -> None:
    """Report a GitHub Actions error annotation and stop the check."""
    print(f"::error::{message}")
    raise SystemExit(1)


def find_wheel() -> Path:
    """Return the sole built wheel, failing when ``dist`` is ambiguous."""
    wheels = sorted(Path("dist").glob("*.whl"))
    if len(wheels) != 1:
        fail(f"expected exactly one wheel in dist/, found {len(wheels)}")
    return wheels[0]


def normalise(requirement: str) -> str:
    """The PEP 503 name a requirement line names, dropping version and markers."""
    match = NAME.match(requirement.strip())
    return re.sub(r"[-_.]+", "-", match.group()).lower() if match else ""


def check_metadata(wheel: Path) -> None:
    """The wheel requires both parser packages, with no extra gating them."""
    with zipfile.ZipFile(wheel) as zf:
        names = [n for n in zf.namelist() if n.endswith(".dist-info/METADATA")]
        if len(names) != 1:
            fail(f"{wheel.name} has {len(names)} METADATA files")
        metadata = BytesParser().parsebytes(zf.read(names[0]))

    if metadata.get_all("Provides-Extra"):
        fail(f"{wheel.name} still declares extras: {metadata.get_all('Provides-Extra')}")

    requirements = metadata.get_all("Requires-Dist") or []
    for package in REQUIRED:
        matching = [r for r in requirements if normalise(r) == package]
        if not matching:
            fail(f"{wheel.name} does not require {package}")
        gated = [r for r in matching if ";" in r]
        if gated:
            fail(f"{wheel.name} requires {package} only conditionally: {gated[0]}")
    print(f"{wheel.name} requires {' and '.join(REQUIRED)} unconditionally")


def check_clean_install(wheel: Path) -> None:
    """A throwaway environment holding only the wheel has a working parser."""
    result = subprocess.run(
        (
            "uv",
            "run",
            "--isolated",
            "--no-project",
            "--with",
            str(wheel),
            "python",
            "-c",
            CLEAN_INSTALL_CHECK,
        ),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        fail(
            "a clean install of the wheel has no AST context: "
            f"{(result.stderr or result.stdout).strip().splitlines()[-1:]}"
        )
    print(f"a clean install of {wheel.name} has AST context with no extras named")


def main() -> int:
    """Validate the built wheel's metadata and clean-install behavior."""
    wheel = find_wheel()
    check_metadata(wheel)
    check_clean_install(wheel)
    return 0


if __name__ == "__main__":
    sys.exit(main())
