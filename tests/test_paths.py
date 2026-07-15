"""Single-source-of-truth path containment (harness.paths.path_within).

Guards the boundary case the audit flagged: two near-identical containment
checks that disagreed on whether the parent dir is "inside" itself. is_safe_path
(file tools) allows it; _is_confined (worktree confinement) forbids it. Both now
delegate to path_within, so the difference is one explicit flag, not a fork.
"""
import os
import subprocess
import tempfile

from harness.paths import (
    git_toplevel,
    is_git_restricted_path,
    path_within,
    resolve_workspace_path,
)
from harness.conversation import is_safe_path
from harness.worktrees import _is_confined


def _git_init(repo: str) -> None:
    subprocess.run(
        ["git", "init"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )


def test_path_within_boundary_semantics():
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.realpath(tmp)
        child = os.path.join(root, "sub", "file.txt")
        outside = os.path.join(root, "..", "escape.txt")

        # nested path is inside under both semantics
        assert path_within(child, root, allow_equal=True) is True
        assert path_within(child, root, allow_equal=False) is True

        # the boundary (path == parent) is the ONLY difference
        assert path_within(root, root, allow_equal=True) is True
        assert path_within(root, root, allow_equal=False) is False

        # escaping the parent is rejected either way
        assert path_within(outside, root, allow_equal=True) is False
        assert path_within(outside, root, allow_equal=False) is False


def test_is_safe_path_allows_workspace_root():
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.realpath(tmp)
        assert is_safe_path(root, root) is True                      # file tools: root is valid
        assert is_safe_path(os.path.join(root, "a.py"), root) is True
        assert is_safe_path("/etc/passwd", root) is False


def test_is_confined_rejects_boundary():
    with tempfile.TemporaryDirectory() as tmp:
        managed = os.path.realpath(tmp)
        assert _is_confined(managed, managed) is False               # confinement: strictly inside only
        assert _is_confined(os.path.join(managed, "wt-1"), managed) is True
        assert _is_confined("/tmp", managed) is False


def test_path_within_never_raises_on_bad_input():
    # An unresolvable comparison must fail closed, not explode.
    assert path_within("relative/path", "/abs/parent", allow_equal=True) is False


def test_resolve_workspace_path_relative_and_absolute():
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.realpath(tmp)
        nested = os.path.join(root, "sub", "file.txt")
        os.makedirs(os.path.dirname(nested))
        with open(nested, "w", encoding="utf-8") as f:
            f.write("ok")

        abs_path, rel = resolve_workspace_path(root, "sub/file.txt")
        assert os.path.normcase(abs_path) == os.path.normcase(nested)
        assert rel.replace("\\", "/") == "sub/file.txt"

        # Absolute path under the repo must resolve (Windows-style abs included).
        abs_path2, rel2 = resolve_workspace_path(root, nested)
        assert os.path.normcase(abs_path2) == os.path.normcase(nested)
        assert rel2.replace("\\", "/") == "sub/file.txt"

        # Forward-slash absolute form
        fwd = nested.replace("\\", "/")
        abs_path3, rel3 = resolve_workspace_path(root, fwd)
        assert os.path.normcase(abs_path3) == os.path.normcase(nested)
        assert rel3.replace("\\", "/") == "sub/file.txt"


def test_resolve_workspace_path_denies_escape():
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.realpath(tmp)
        outside = os.path.realpath(os.path.join(root, "..", "escape-outside.txt"))
        try:
            resolve_workspace_path(root, "../escape-outside.txt")
            assert False, "expected escape to raise"
        except ValueError as e:
            assert "escapes workspace" in str(e)

        try:
            resolve_workspace_path(root, outside)
            assert False, "expected absolute outside path to raise"
        except ValueError as e:
            assert "escapes workspace" in str(e)


def test_resolve_workspace_path_windows_abs_under_repo_casing():
    """Absolute-under-repo must succeed even when drive/component casing differs."""
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.realpath(tmp)
        child = os.path.join(root, "a", "b.txt")
        os.makedirs(os.path.dirname(child))
        with open(child, "w", encoding="utf-8") as f:
            f.write("x")

        if os.name == "nt":
            mixed = child[0].swapcase() + child[1:]
            abs_path, rel = resolve_workspace_path(root, mixed)
            assert os.path.normcase(abs_path) == os.path.normcase(child)
            assert rel.replace("\\", "/") == "a/b.txt"
        else:
            abs_path, rel = resolve_workspace_path(root, child)
            assert abs_path == child
            assert rel == "a/b.txt"


def test_is_git_restricted_path():
    assert is_git_restricted_path(".git/config") is True
    assert is_git_restricted_path("src/.git/hooks") is True
    assert is_git_restricted_path(".gitignore") is False
    assert is_git_restricted_path(".github/workflows/ci.yml") is False
    assert is_git_restricted_path("src/file.py") is False


def test_git_toplevel_none_outside_work_tree():
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.realpath(tmp)
        assert git_toplevel(root) is None
        assert git_toplevel("") is None


def test_git_toplevel_nested_workspace():
    """Nested workspace under a clone reports the clone root (Windows casing ok)."""
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.realpath(tmp)
        _git_init(root)
        nested = os.path.join(root, "Ashita", "addons", "kotoba")
        os.makedirs(nested)

        top = git_toplevel(nested)
        assert top is not None
        assert os.path.normcase(top) == os.path.normcase(root)

        # Same answer from the toplevel itself; cache must stay consistent.
        assert os.path.normcase(git_toplevel(root) or "") == os.path.normcase(root)

        # Forward-slash form (agent-reported Windows paths) still resolves.
        fwd = nested.replace("\\", "/")
        top_fwd = git_toplevel(fwd)
        assert top_fwd is not None
        assert os.path.normcase(top_fwd) == os.path.normcase(root)


def test_nested_read_allow_via_path_within():
    """Parent README under the same git root is inside the toplevel root."""
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.realpath(tmp)
        _git_init(root)
        nested = os.path.join(root, "sub", "ws")
        os.makedirs(nested)
        readme = os.path.join(root, "README.md")
        with open(readme, "w", encoding="utf-8") as f:
            f.write("# parent\n")

        top = git_toplevel(nested)
        assert top is not None
        assert path_within(readme, top, allow_equal=True) is True
        assert path_within(readme, nested, allow_equal=True) is False
        outside = os.path.realpath(os.path.join(root, "..", "escape-outside.txt"))
        assert path_within(outside, top, allow_equal=True) is False
