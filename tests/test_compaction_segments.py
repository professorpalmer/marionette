"""Bounded durable generations, using actual archive files and product paging."""
import json
from pathlib import Path

import pytest

from harness import compaction_archive as archive
from harness.history_compaction_journal import commit_compacted_transcript
from harness.sessions import load_transcript


def test_complete_append_chunks_middle_over_message_cap(tmp_path):
    """One Compact of 400+ rows must archive every row, not abort."""
    state = str(tmp_path)
    rows = [{"role": "user", "content": f"fat-{i}"} for i in range(archive.ARCHIVE_MAX_MESSAGES + 50)]
    assert archive.append_compaction_archive(
        state, "fat", rows, require_complete=True, commit_id="fat-one",
    )
    page, total = archive.load_compaction_archive_page(state, "fat", offset=0, limit=3)
    assert total == len(rows)
    assert page == rows[:3]
    last, last_total = archive.load_compaction_archive_page(
        state, "fat", offset=len(rows) - 2, limit=2,
    )
    assert last_total == len(rows)
    assert last == rows[-2:]


def test_complete_append_refuses_a_single_row_over_the_byte_cap(tmp_path):
    state = str(tmp_path)
    huge = [{"role": "user", "content": "x" * (archive.ARCHIVE_MAX_SERIALIZED_BYTES + 100)}]
    assert not archive.append_compaction_archive(
        state, "huge", huge, require_complete=True, commit_id="too-big",
    )
    assert archive.load_compaction_archive_page(state, "huge") == ([], 0)


def test_commit_compacted_transcript_fat_middle(tmp_path):
    state = str(tmp_path)
    rows = [{"role": "assistant", "content": f"turn-{i}"} for i in range(450)]
    source = {"history": rows}
    target = {"history": [{"role": "assistant", "content": "summary"}]}
    commit_compacted_transcript(state, "fat-commit", source, target, rows)
    assert load_transcript(state, "fat-commit") == target
    page, total = archive.load_compaction_archive_page(state, "fat-commit", offset=0, limit=1)
    assert total == 450
    assert page == rows[:1]


def test_repeated_commits_retain_every_row_and_page(tmp_path):
    state = str(tmp_path)
    expected = []
    for wave in range(12):
        rows = [{'role': 'user', 'content': f'exact-{wave}-{i}'} for i in range(40)]
        source = {'history': rows}
        target = {'history': [{'role': 'assistant', 'content': f'summary-{wave}'}]}
        commit_compacted_transcript(state, 'paged', source, target, rows)
        expected.extend(rows)
    assert load_transcript(state, 'paged') == target
    for offset in range(0, len(expected), 7):
        page, total = archive.load_compaction_archive_page(state, 'paged', offset=offset, limit=7)
        assert total == 480
        assert page == expected[offset:offset + 7]
    assert len(archive.load_compaction_archive_messages(state, 'paged')) <= archive.ARCHIVE_MAX_MESSAGES


def test_recovery_after_archive_publication_does_not_duplicate_peek(tmp_path):
    from bench.compaction_durable import run_case, SID, session_at, FACTS
    from harness.pilot import PilotAction
    run_case(tmp_path, 'after_archive')
    session = session_at(str(tmp_path))
    session.load_history(load_transcript(str(tmp_path), SID))
    bodies = [session._do_peek_history(PilotAction(kind='peek_history', arguments={'offset': i, 'limit': 4}))[2]
              for i in range(0, 60, 4)]
    for fact in FACTS.values():
        assert '\n'.join(bodies).count(fact) == 1


@pytest.mark.parametrize('damage', ['missing', 'corrupt'])
def test_old_segment_damage_refuses_append_and_page(tmp_path, damage):
    state = str(tmp_path)
    rows = [{'role': 'user', 'content': 'oldest'}]
    assert archive.append_compaction_archive(state, 's', rows, require_complete=True, commit_id='first')
    manifest_path = Path(archive.compaction_archive_path(state, 's'))
    first = json.loads(manifest_path.read_text())
    assert archive.append_compaction_archive(state, 's', [{'role': 'assistant', 'content': 'newest'}],
                                             require_complete=True, commit_id='second')
    segment = Path(str(manifest_path) + '.segments') / (first['head'] + '.json')
    if damage == 'missing':
        segment.unlink()
    else:
        segment.write_text('{}')
    before = manifest_path.read_bytes()
    with pytest.raises((OSError, ValueError)):
        archive.load_compaction_archive_page(state, 's', offset=1, limit=1)
    assert not archive.append_compaction_archive(state, 's', rows, require_complete=True, commit_id='third')
    assert manifest_path.read_bytes() == before


def test_legacy_migration_preserves_rows_and_role_offsets(tmp_path):
    state = str(tmp_path)
    original = [{'role': 'user', 'content': f'legacy-{i}'} for i in range(400)]
    assert archive.append_compaction_archive(state, 's', original)
    assert archive.append_compaction_archive(state, 's', [{'role': 'assistant', 'content': 'new'}],
                                             require_complete=True, commit_id='new')
    assert archive.load_compaction_archive_page(state, 's', offset=399) == (original[399:] + [{'role': 'assistant', 'content': 'new'}], 401)
    assert archive.load_compaction_archive_page(state, 's', role='assistant') == ([{'role': 'assistant', 'content': 'new'}], 1)


def test_pending_recovery_verifies_exact_generation(tmp_path):
    import subprocess
    import sys
    from bench.compaction_durable import SID
    result = subprocess.run([sys.executable, '-m', 'bench.compaction_durable', '--child', 'crash',
                             '--state', str(tmp_path), '--boundary', 'after_transcript_replace'], timeout=30)
    assert result.returncode == 71
    state = str(tmp_path)
    # Later publication must not invalidate the immutable generation in the journal.
    assert archive.append_compaction_archive(state, SID, [{'role': 'user', 'content': 'later-generation'}],
                                             require_complete=True, commit_id='later')
    assert any(row.get('_compressed_summary') for row in load_transcript(state, SID)['history'])


def test_repeated_product_compactions_cold_exact_recall(tmp_path):
    import subprocess
    import sys
    result = subprocess.run([sys.executable, '-m', 'bench.compaction_durable', '--child', 'repeated',
                             '--state', str(tmp_path)], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report['archive_rows'] > 400
    assert report['exact_facts'] == report['expected_facts'] == 600
    assert report['oldest_exact'] and report['newest_exact']
    assert report['max_peek_bytes'] < 8300


def test_cumulative_bytes_exceed_cap_without_unbounded_reads(tmp_path, monkeypatch):
    import builtins
    state = str(tmp_path)
    expected = []
    for wave in range(3):
        rows = [{'role': 'user', 'content': f'{wave}-{i}:' + 'x' * 30000} for i in range(100)]
        assert archive.append_compaction_archive(state, 'bytes', rows, require_complete=True, commit_id=str(wave))
        expected.extend(rows)
    assert len(json.dumps(expected).encode()) > archive.ARCHIVE_MAX_SERIALIZED_BYTES
    real_open = builtins.open
    sizes = []
    class BoundedReader:
        def __init__(self, handle):
            self.handle = handle
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.handle.close()
        def read(self, size=-1):
            assert 0 < size <= archive.ARCHIVE_LOAD_MAX_BYTES + 1
            sizes.append(size)
            return self.handle.read(size)
    def checked(path, *args, **kwargs):
        handle = real_open(path, *args, **kwargs)
        if Path(path).parent.name.endswith('.segments') and args and args[0] == 'rb':
            return BoundedReader(handle)
        return handle
    monkeypatch.setattr(builtins, 'open', checked)
    for offset in (0, 99, 199, 299):
        page, total = archive.load_compaction_archive_page(state, 'bytes', offset=offset, limit=1)
        assert page == expected[offset:offset + 1]
        assert total == 300
    assert sizes


@pytest.mark.parametrize('boundary', ['segment_published', 'manifest_published'])
def test_abrupt_exit_during_segment_publication(tmp_path, boundary):
    import subprocess
    import sys
    script = '''
import os, sys
from pathlib import Path
from harness import compaction_archive as archive
from harness.history_compaction_journal import commit_compacted_transcript
state, boundary = sys.argv[1:]
replace = os.replace
def crash(src, dst):
    replace(src, dst)
    if (boundary == 'segment_published' and Path(dst).parent.name.endswith('.segments')) or (boundary == 'manifest_published' and str(dst).endswith('.archive.json')):
        os._exit(73)
os.replace = crash
commit_compacted_transcript(state, 's', {'history': [{'role':'user','content':'unsaved-exact'}]}, {'history': []}, [{'role':'user','content':'unsaved-exact'}])
'''
    result = subprocess.run([sys.executable, '-c', script, str(tmp_path), boundary], timeout=30)
    assert result.returncode == 73
    assert load_transcript(str(tmp_path), 's')['history'] == [{'role': 'user', 'content': 'unsaved-exact'}]
    assert archive.load_compaction_archive_page(str(tmp_path), 's') == ([], 0)
    assert load_transcript(str(tmp_path), 's')['history'][0]['content'] == 'unsaved-exact'


@pytest.mark.parametrize('damage', ['missing', 'older'])
def test_pending_generation_republishes_missing_manifest(tmp_path, damage):
    import subprocess
    import sys
    from bench.compaction_durable import SID
    result = subprocess.run([sys.executable, '-m', 'bench.compaction_durable', '--child', 'crash',
                             '--state', str(tmp_path), '--boundary', 'after_transcript_replace'], timeout=30)
    assert result.returncode == 71
    manifest = Path(archive.compaction_archive_path(str(tmp_path), SID))
    if damage == 'missing':
        manifest.unlink()
    else:
        manifest.write_text(json.dumps({'version': 2, 'session_id': SID, 'head': '', 'total': 0, 'commit_id': ''}))
    assert any(row.get('_compressed_summary') for row in load_transcript(str(tmp_path), SID)['history'])
    assert archive.load_compaction_archive_page(str(tmp_path), SID)[1] == 18
