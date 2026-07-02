"""Single-source-of-truth path containment (harness.paths.path_within).

Guards the boundary case the audit flagged: two near-identical containment
checks that disagreed on whether the parent dir is "inside" itself. is_safe_path
(file tools) allows it; _is_confined (worktree confinement) forbids it. Both now
delegate to path_within, so the difference is one explicit flag, not a fork.
"""
import os
import tempfile

from harness.paths import path_within
from harness.conversation import is_safe_path
from harness.worktrees import _is_confined


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
