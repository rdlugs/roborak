"""Reviewing a plain directory.

The important tests here are the ones about what is *not* reviewed: a walk that
descends into ``node_modules`` or hands a model a binary blob is worse than no
directory review at all, and a walk that quietly drops files lies about coverage.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from roborak.core.config import DEFAULT_IGNORE_PATHS
from roborak.sources.base import SourceError
from roborak.sources.paths import PathsSource


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    root = tmp_path / "codebase"
    (root / "app").mkdir(parents=True)
    (root / "app" / "core.py").write_text("def run():\n    return 1\n", encoding="utf-8")
    (root / "README.md").write_text("# Codebase\n", encoding="utf-8")
    return root


def test_a_directory_without_git_becomes_a_changeset(tree: Path):
    changeset = PathsSource(root=tree).load()
    assert changeset.origin == "paths"
    assert {file.path for file in changeset.files} == {"app/core.py", "README.md"}


def test_every_file_is_reviewed_whole(tree: Path):
    """Whole-file review means every line is anchorable, with no diff to fall outside of."""
    changeset = PathsSource(root=tree).load()
    core = changeset.file_by_path("app/core.py")
    assert core is not None
    assert core.change_type == "added"
    assert core.language == "python"
    assert core.new_content == "def run():\n    return 1\n"
    assert core.added_lines == {1, 2}
    assert core.diff_position(1) == 2


def test_windows_line_endings_are_normalised(tree: Path):
    """A CRLF file must reach the IR spelling its line endings the way every other
    source does, or the same file reviewed on Windows and Linux is two files."""
    (tree / "crlf.py").write_bytes(b"def run():\r\n    return 1\r\n")
    crlf = PathsSource(root=tree).load().file_by_path("crlf.py")
    assert crlf is not None
    assert crlf.new_content == "def run():\n    return 1\n"
    assert crlf.added_lines == {1, 2}


def test_a_subdirectory_reviews_only_that_subtree(tree: Path):
    changeset = PathsSource(root=tree / "app").load()
    assert {file.path for file in changeset.files} == {"core.py"}


def test_dependency_and_vcs_directories_are_never_walked(tree: Path):
    (tree / "node_modules" / "left-pad").mkdir(parents=True)
    (tree / "node_modules" / "left-pad" / "index.js").write_text("x", encoding="utf-8")
    (tree / ".git").mkdir()
    (tree / ".git" / "config").write_text("[core]\n", encoding="utf-8")
    (tree / "__pycache__").mkdir()
    (tree / "__pycache__" / "core.cpython-312.pyc").write_text("x", encoding="utf-8")

    paths = {file.path for file in PathsSource(root=tree).load().files}
    assert paths == {"app/core.py", "README.md"}


def test_configured_ignore_paths_are_respected(tree: Path):
    (tree / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    changeset = PathsSource(root=tree, ignore_paths=list(DEFAULT_IGNORE_PATHS)).load()
    assert changeset.file_by_path("uv.lock") is None


def test_a_binary_file_is_flagged_rather_than_read(tree: Path):
    (tree / "logo.png").write_bytes(b"\x89PNG\x00\x01\x02binary")
    changeset = PathsSource(root=tree).load()
    logo = changeset.file_by_path("logo.png")
    assert logo is not None
    assert logo.is_binary
    assert logo.new_content is None


def test_an_oversized_file_is_reported_as_omitted(tree: Path):
    (tree / "huge.py").write_text("x = 1\n" * 500, encoding="utf-8")
    changeset = PathsSource(root=tree, max_file_bytes=64).load()
    assert changeset.file_by_path("huge.py") is None
    assert "huge.py" in changeset.omitted_files


def test_a_file_removed_after_the_walk_is_reported_as_omitted(
    tree: Path, monkeypatch: pytest.MonkeyPatch
):
    source = PathsSource(root=tree)
    eligible_paths = source._eligible_paths

    def remove_file_after_walk(changeset):
        paths = eligible_paths(changeset)
        (tree / "README.md").unlink()
        return paths

    monkeypatch.setattr(source, "_eligible_paths", remove_file_after_walk)
    changeset = source.load()

    assert changeset.file_by_path("README.md") is None
    assert "README.md" in changeset.omitted_files


def test_a_file_read_failure_is_reported_as_omitted(tree: Path, monkeypatch: pytest.MonkeyPatch):
    denied = tree / "README.md"
    read_bytes = Path.read_bytes

    def deny_read(path: Path):
        if path == denied:
            raise PermissionError("not permitted")
        return read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", deny_read)
    changeset = PathsSource(root=tree).load()

    assert changeset.file_by_path("README.md") is None
    assert "README.md" in changeset.omitted_files


def test_the_file_cap_omits_the_surplus_instead_of_hiding_it(tree: Path):
    for index in range(5):
        (tree / f"mod{index}.py").write_text("x = 1\n", encoding="utf-8")
    changeset = PathsSource(root=tree, max_files=3).load()
    assert len(changeset.files) == 3
    assert len(changeset.omitted_files) == 4


def test_an_empty_directory_is_empty_not_an_error(tmp_path: Path):
    empty = tmp_path / "empty"
    empty.mkdir()
    assert PathsSource(root=empty).load().is_empty


def test_a_missing_path_is_an_error(tmp_path: Path):
    with pytest.raises(SourceError, match="does not exist"):
        PathsSource(root=tmp_path / "nope").load()


def test_a_file_is_not_a_directory(tree: Path):
    with pytest.raises(SourceError, match="not a directory"):
        PathsSource(root=tree / "README.md").load()


@pytest.mark.skipif(
    os.name == "nt" or os.geteuid() == 0,
    reason="permission bits do not stop root, and mean something else on Windows",
)
def test_an_unreadable_directory_is_an_error(tmp_path: Path):
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o000)
    try:
        with pytest.raises(SourceError, match="not readable"):
            PathsSource(root=locked).load()
    finally:
        locked.chmod(0o755)


@pytest.mark.skipif(
    os.name == "nt" or os.geteuid() == 0,
    reason="permission bits do not stop root, and mean something else on Windows",
)
def test_an_unreadable_nested_directory_is_an_error(tree: Path):
    locked = tree / "locked"
    locked.mkdir()
    locked.chmod(0o000)
    try:
        with pytest.raises(SourceError, match="Could not scan"):
            PathsSource(root=tree).load()
    finally:
        locked.chmod(0o755)


def test_symlinked_directories_are_not_followed(tree: Path):
    (tree / "loop").symlink_to(tree, target_is_directory=True)
    paths = {file.path for file in PathsSource(root=tree).load().files}
    assert paths == {"app/core.py", "README.md"}
