"""Tests for CodeGraph staleness detection. The original bug: deletions left the
index referencing ghost files but _codegraph_is_stale returned False because no
SURVIVING file looked newer. The fix adds directory-mtime checks. These tests
prove edits, additions, AND deletions are all detected.

Wall-clock sleeps are avoided: mtimes are set with os.utime. Timestamps are
spaced by tens of seconds so FAT/2s-resolution volumes (Windows CI temps) still
see a strict ordering.
"""
import os

from harness.server import _codegraph_is_stale

# Fixed epochs — far enough apart for 2-second filesystem mtime rounding.
_SOURCE_T = 1_700_000_000.0
_INDEX_T = 1_700_000_100.0
_NEWER_T = 1_700_000_200.0


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
    for path in (repo, repo / "src", repo / "src" / "a.py", repo / "src" / "b.py"):
        _set_mtime(path, _SOURCE_T)
    if with_index:
        _set_mtime(repo / ".codegraph", _INDEX_T)
        _set_mtime(repo / ".codegraph" / "db", _INDEX_T)
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
    (repo / "src" / "a.py").write_text("print('a edited')\n")
    _set_mtime(repo / "src" / "a.py", _NEWER_T)
    assert _codegraph_is_stale(str(repo)) is True


def test_added_file_is_stale(tmp_path):
    repo = _mk_repo(tmp_path)
    (repo / "src" / "c.py").write_text("print('c')\n")
    _set_mtime(repo / "src" / "c.py", _NEWER_T)
    _set_mtime(repo / "src", _NEWER_T)
    assert _codegraph_is_stale(str(repo)) is True


def test_deleted_file_is_stale(tmp_path):
    # THE REGRESSION: deleting a file must mark the index stale even though no
    # surviving file is newer. Caught via the parent directory's bumped mtime.
    repo = _mk_repo(tmp_path)
    os.remove(repo / "src" / "b.py")
    _set_mtime(repo / "src", _NEWER_T)
    assert _codegraph_is_stale(str(repo)) is True


def test_edits_inside_skipped_dirs_do_not_descend(tmp_path):
    # We do not DESCEND into __pycache__/node_modules to inspect their contents
    # (those churn constantly). A pre-existing skipped dir whose internal files
    # change must not, by itself, trigger staleness.
    repo = _mk_repo(tmp_path)
    pc = repo / "node_modules"
    pc.mkdir()
    (pc / "junk.js").write_text("old")
    # mkdir(node_modules) bumps repo mtime — pull sources/index back into order.
    _set_mtime(pc, _SOURCE_T)
    _set_mtime(pc / "junk.js", _SOURCE_T)
    _set_mtime(repo, _SOURCE_T)
    _set_mtime(repo / "src", _SOURCE_T)
    _set_mtime(repo / ".codegraph", _INDEX_T)
    _set_mtime(repo / ".codegraph" / "db", _INDEX_T)
    # mutate a file *inside* the skipped dir only (and its dir mtime)
    (pc / "junk.js").write_text("changed")
    _set_mtime(pc / "junk.js", _NEWER_T)
    _set_mtime(pc, _NEWER_T)
    # Creating/changing under node_modules can bump repo mtime on some FS;
    # keep the walked roots older than the index so only skipped-dir churn remains.
    _set_mtime(repo, _SOURCE_T)
    _set_mtime(repo / "src", _SOURCE_T)
    # node_modules is pruned from the walk, so its churn is invisible. The repo
    # root + src dirs are unchanged -> not stale.
    assert _codegraph_is_stale(str(repo)) is False


def test_bias_is_toward_detecting_change(tmp_path):
    # Documented design choice: a false-positive reindex (cheap, debounced,
    # background) is preferable to a false-negative stale index (misleads the
    # pilot). Any real source-tree mutation should be detected.
    repo = _mk_repo(tmp_path)
    (repo / "src" / "new_module.py").write_text("x = 1" + chr(10))
    _set_mtime(repo / "src" / "new_module.py", _NEWER_T)
    _set_mtime(repo / "src", _NEWER_T)
    assert _codegraph_is_stale(str(repo)) is True
