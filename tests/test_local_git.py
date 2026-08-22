"""Tests against a real git repository.

The important one is ``test_line_numbers_match_the_working_tree``: it checks our
computed line numbers against ground truth rather than against our own parser, so
it would catch a mapping bug that a hand-written fixture agreed with.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from roborak.sources.base import SourceError
from roborak.sources.local_git import LocalGitSource, Scope


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True)
    return result.stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "config", "user.email", "test@example.com")
    git(tmp_path, "config", "user.name", "Test")

    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "core.py").write_text(
        "\n".join(f"line_{i} = {i}" for i in range(1, 41)) + "\n"
    )
    (tmp_path / "README.md").write_text("# demo\n")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-q", "-m", "initial")
    return tmp_path


def test_no_changes_is_empty(repo: Path):
    changeset = LocalGitSource(repo=repo, scope=Scope.UNCOMMITTED).load()
    assert changeset.is_empty


def test_not_a_repo_raises(tmp_path: Path):
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(SourceError, match="not a git repository"):
        LocalGitSource(repo=plain).load()


def test_uncommitted_edit_is_picked_up(repo: Path):
    path = repo / "app" / "core.py"
    lines = path.read_text(encoding="utf-8").splitlines()
    lines.insert(10, "inserted = True")
    path.write_text("\n".join(lines) + "\n")

    changeset = LocalGitSource(repo=repo, scope=Scope.UNCOMMITTED).load()
    file = changeset.file_by_path("app/core.py")
    assert file is not None
    assert file.change_type == "modified"
    assert file.language == "python"
    assert file.added_lines == {11}
    assert file.new_content is not None


def test_committed_scope_needs_a_base(repo: Path):
    with pytest.raises(SourceError, match="needs a base"):
        LocalGitSource(repo=repo, scope=Scope.COMMITTED).load()


def test_branch_against_base(repo: Path):
    git(repo, "checkout", "-q", "-b", "feature")
    (repo / "app" / "new.py").write_text("def added():\n    return 1\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "add file")

    changeset = LocalGitSource(repo=repo, scope=Scope.COMMITTED, base="main").load()
    file = changeset.file_by_path("app/new.py")
    assert file is not None
    assert file.change_type == "added"
    assert file.added_lines == {1, 2}
    assert changeset.head_ref == "feature"


def test_untracked_files_are_excluded_by_default(repo: Path):
    (repo / "app" / "scratch.py").write_text("x = 1\ny = 2\n")
    changeset = LocalGitSource(repo=repo, scope=Scope.UNCOMMITTED).load()
    assert changeset.file_by_path("app/scratch.py") is None


def test_untracked_files_are_included_on_request(repo: Path):
    (repo / "app" / "scratch.py").write_text("x = 1\ny = 2\n")
    changeset = LocalGitSource(repo=repo, scope=Scope.UNCOMMITTED, include_untracked=True).load()
    file = changeset.file_by_path("app/scratch.py")
    assert file is not None
    assert file.change_type == "added"
    assert file.added_lines == {1, 2}
    assert file.diff_position(1) == 2


def test_deleted_file_is_reported(repo: Path):
    (repo / "README.md").unlink()
    changeset = LocalGitSource(repo=repo, scope=Scope.UNCOMMITTED).load()
    file = changeset.file_by_path("README.md")
    assert file is not None
    assert file.change_type == "deleted"


def test_rename_is_reported(repo: Path):
    git(repo, "mv", "app/core.py", "app/renamed.py")
    changeset = LocalGitSource(repo=repo, scope=Scope.UNCOMMITTED).load()
    file = changeset.file_by_path("app/renamed.py")
    assert file is not None
    assert file.change_type == "renamed"
    assert file.previous_path == "app/core.py"


def test_line_numbers_match_the_working_tree(repo: Path):
    """Ground truth: every non-removed diff line must equal the real file line.

    This is the invariant that keeps inline comments off the wrong lines. It is
    checked against the file on disk, not against our own parse, so a systematic
    off-by-one in the parser cannot hide here.
    """
    path = repo / "app" / "core.py"
    lines = path.read_text(encoding="utf-8").splitlines()
    lines.insert(3, "early = 1")
    lines[20] = "changed_line = 'x'"
    del lines[30]
    lines.append("appended = True")
    path.write_text("\n".join(lines) + "\n")

    (repo / "app" / "second.py").write_text("a = 1\nb = 2\nc = 3\n")
    git(repo, "add", "app/second.py")

    changeset = LocalGitSource(repo=repo, scope=Scope.UNCOMMITTED).load()
    assert len(changeset.files) >= 2

    checked = 0
    for file in changeset.files:
        assert file.new_content is not None
        actual_lines = file.new_content.splitlines()
        for hunk in file.hunks:
            lineno = hunk.new_start
            for raw in hunk.content.splitlines():
                if raw.startswith(("\\", "-")):
                    continue
                expected = raw[1:] if raw[:1] in "+ " else raw
                assert lineno <= len(actual_lines), f"{file.path}:{lineno} past EOF"
                assert actual_lines[lineno - 1] == expected, (
                    f"{file.path}:{lineno} diff says {expected!r}, "
                    f"file has {actual_lines[lineno - 1]!r}"
                )
                checked += 1
                lineno += 1

    assert checked > 20, "the fixture should exercise a meaningful number of lines"


def test_added_lines_are_exactly_the_plus_lines(repo: Path):
    path = repo / "app" / "core.py"
    lines = path.read_text(encoding="utf-8").splitlines()
    lines.insert(5, "one = 1")
    lines.insert(20, "two = 2")
    path.write_text("\n".join(lines) + "\n")

    changeset = LocalGitSource(repo=repo, scope=Scope.UNCOMMITTED).load()
    file = changeset.file_by_path("app/core.py")
    assert file is not None
    content = file.new_content.splitlines()  # type: ignore[union-attr]
    for lineno in file.added_lines:
        assert content[lineno - 1] in {"one = 1", "two = 2"}
