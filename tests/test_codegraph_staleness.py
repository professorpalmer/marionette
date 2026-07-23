"""Tests for CodeGraph staleness detection. The original bug: deletions left the
index referencing ghost files but _codegraph_is_stale returned False because no
SURVIVING file looked newer. The fix adds directory-mtime checks. These tests
prove edits, additions, AND deletions are all detected.

Wall-clock sleeps are avoided: mtimes are set with os.utime so ordering is
deterministic and the module stays under a second.
"""
import os
import time

from harness.server import _codegraph_is_stale


def _set_mtime(path, when):
    os.utime(os.fspath(path), (when, when))


def _mk_repo(tmp_path, with_index=True):
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "a.py").write_text("print('a')\n")
    (repo / "src" / "b.py").write_text("print('b')\n")
    if with_index:
        (repo / ".codegraph").mkdir()
        (repo / ".codegraph" / "db").write_text("index")
    # Stamp AFTER creating .codegraph — mkdir bumps the repo mtime, which would
    # otherwise look newer than the index and falsely report stale.
    index_t = time.time()
    source_t = index_t - 50.0
    for path in (repo, repo / "src", repo / "src" / "a.py", repo / "src" / "b.py"):
        _set_mtime(path, source_t)
    if with_index:
        _set_mtime(repo / ".codegraph", index_t)
        _set_mtime(repo / ".codegraph" / "db", index_t)
    return repo


def test_fresh_index_not_stale(tmp_path):
    repo = _mk_repo(tmp_path)
    assert _codegraph_is_stale(str(repo)) is False


def test_no_index_not_stale(tmp_path):
    # no .codegraph at all -> not "stale" (nothing to refresh; init handles it)
    repo = _mk_repo(tmp_path, with_index=False)
    assert _codegraph_is_stale(str(repo)) is False


def test_edited_file_is_stale(tmp_path):
    repo = _mk_repo(tmp_path)
    newer = time.time()
    (repo / "src" / "a.py").write_text("print('a edited')\n")
    _set_mtime(repo / "src" / "a.py", newer)
    assert _codegraph_is_stale(str(repo)) is True


def test_added_file_is_stale(tmp_path):
    repo = _mk_repo(tmp_path)
    newer = time.time()
    (repo / "src" / "c.py").write_text("print('c')\n")
    _set_mtime(repo / "src" / "c.py", newer)
    _set_mtime(repo / "src", newer)
    assert _codegraph_is_stale(str(repo)) is True


def test_deleted_file_is_stale(tmp_path):
    # THE REGRESSION: deleting a file must mark the index stale even though no
    # surviving file is newer. Caught via the parent directory's bumped mtime.
    repo = _mk_repo(tmp_path)
    newer = time.time()
    os.remove(repo / "src" / "b.py")
    _set_mtime(repo / "src", newer)
    assert _codegraph_is_stale(str(repo)) is True


def test_edits_inside_skipped_dirs_do_not_descend(tmp_path):
    # We do not DESCEND into __pycache__/node_modules to inspect their contents
    # (those churn constantly). A pre-existing skipped dir whose internal files
    # change must not, by itself, trigger staleness.
    repo = _mk_repo(tmp_path)
    pc = repo / "node_modules"
    pc.mkdir()
    (pc / "junk.js").write_text("old")
    # Keep skipped-dir churn older than the index; bump index past everything.
    older = time.time() - 10.0
    index_t = time.time()
    _set_mtime(pc, older)
    _set_mtime(pc / "junk.js", older)
    _set_mtime(repo / ".codegraph", index_t)
    _set_mtime(repo / ".codegraph" / "db", index_t)
    # mutate a file *inside* the skipped dir only (and its dir mtime)
    (pc / "junk.js").write_text("changed")
    _set_mtime(pc / "junk.js", time.time() + 5.0)
    _set_mtime(pc, time.time() + 5.0)
    # node_modules is pruned from the walk, so its churn is invisible. The repo
    # root + src dirs are unchanged -> not stale.
    assert _codegraph_is_stale(str(repo)) is False


def test_bias_is_toward_detecting_change(tmp_path):
    # Documented design choice: a false-positive reindex (cheap, debounced,
    # background) is preferable to a false-negative stale index (misleads the
    # pilot). Any real source-tree mutation should be detected.
    repo = _mk_repo(tmp_path)
    newer = time.time()
    (repo / "src" / "new_module.py").write_text("x = 1" + chr(10))
    _set_mtime(repo / "src" / "new_module.py", newer)
    _set_mtime(repo / "src", newer)
    assert _codegraph_is_stale(str(repo)) is True
