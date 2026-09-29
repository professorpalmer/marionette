"""Boot sweep removes queue and input files left by deleted sessions."""
import hashlib
import os

from harness.api.sessions import sweep_orphan_session_files


def _files(root, sid):
    h = hashlib.sha256(sid.encode()).hexdigest()
    q = root / 'prompt_queues'
    t = root / 'transcripts'
    q.mkdir(exist_ok=True)
    t.mkdir(exist_ok=True)
    paths = [q / (h + '.json'), q / (h + '.json.lock'),
             t / (sid + '.inputs.json'), t / (sid + '.inputs.json.lock')]
    for p in paths:
        p.write_text('[]')
    (t / (sid + '.input-originals')).mkdir()
    blob = t / (sid + '.input-originals') / 'blob'
    blob.write_text('x')
    return paths + [t / (sid + '.input-originals')]


def _age(paths, seconds):
    for p in paths:
        st = os.stat(p)
        os.utime(p, (st.st_atime - seconds, st.st_mtime - seconds))


def test_sweeps_deleted_sessions_and_keeps_live_ones(tmp_path):
    gone = _files(tmp_path, 'deleted-1')
    live = _files(tmp_path, 'live-1')
    _age(gone + live, 7200)
    (tmp_path / 'transcripts' / 'live-1.json').write_text('[]')
    removed = sweep_orphan_session_files(str(tmp_path), ['live-1'])
    assert removed == 5
    assert not any(p.exists() for p in gone)
    assert all(p.exists() for p in live)
    assert (tmp_path / 'transcripts' / 'live-1.json').exists()


def test_recent_orphans_survive_for_a_sibling_backend(tmp_path):
    fresh = _files(tmp_path, 'just-made')
    assert sweep_orphan_session_files(str(tmp_path), []) == 0
    assert all(p.exists() for p in fresh)


def test_missing_dirs_are_a_no_op(tmp_path):
    assert sweep_orphan_session_files(str(tmp_path / 'nope'), ['a']) == 0
