"""Durable compaction regression tests; no provider or personal state."""
import copy

import pytest

from harness.sessions import load_transcript
from test_turn_compaction_ownership import fat_session


def test_success_is_persisted_before_event(tmp_path, monkeypatch):
    session = fat_session(tmp_path, monkeypatch)
    monkeypatch.setenv('HARNESS_COMPACTION_RESIDUAL', 'catalog')
    events = list(session._maybe_compact_history(force=True))
    assert not events[-1].data.get('aborted')
    persisted = load_transcript(str(tmp_path), 'default')
    assert isinstance(persisted, dict)
    assert persisted['history'] == session._history[1:]


def test_same_length_source_change_aborts(tmp_path, monkeypatch):
    session = fat_session(tmp_path, monkeypatch)
    monkeypatch.setenv('HARNESS_COMPACTION_RESIDUAL', 'catalog')
    run = session._maybe_compact_history(force=True)
    assert next(run).kind == 'compacting'
    session._history[3]['content'] = 'Z' * len(session._history[3]['content'])
    changed = copy.deepcopy(session._history)
    events = list(run)
    assert session._history == changed
    assert events[-1].data['aborted']


def test_archive_retention_cannot_drop_original_middle(tmp_path, monkeypatch):
    from harness.compaction_archive import load_compaction_archive_page

    session = fat_session(tmp_path, monkeypatch)
    monkeypatch.setenv('HARNESS_COMPACTION_RESIDUAL', 'catalog')
    monkeypatch.setattr('harness.compaction_archive.ARCHIVE_MAX_MESSAGES', 3)
    original = copy.deepcopy(session._history)
    original_middle = original[1:]
    events = list(session._maybe_compact_history(force=True))
    assert not events[-1].data.get('aborted')
    assert session._history != original
    page, total = load_compaction_archive_page(str(tmp_path), 'default', offset=0, limit=400)
    assert total == events[-1].data['summarized_messages']
    assert total > 3
    assert page[0] == original_middle[0]
    assert all(row in original_middle for row in page)


def test_changed_turn_generation_aborts(tmp_path, monkeypatch):
    session = fat_session(tmp_path, monkeypatch)
    monkeypatch.setenv('HARNESS_COMPACTION_RESIDUAL', 'catalog')
    run = session._maybe_compact_history(force=True)
    next(run)
    original = copy.deepcopy(session._history)
    session._busy_gen += 1
    events = list(run)
    assert session._history == original
    assert events[-1].data['reason'] == 'source_revision_changed'


def test_concurrent_append_is_preserved_on_disk(tmp_path, monkeypatch):
    session = fat_session(tmp_path, monkeypatch)
    monkeypatch.setenv('HARNESS_COMPACTION_RESIDUAL', 'catalog')
    run = session._maybe_compact_history(force=True)
    next(run)
    session._history.append({'role': 'user', 'content': 'New concurrent tail.'})
    events = list(run)
    assert not events[-1].data.get('aborted')
    assert load_transcript(str(tmp_path), 'default')['history'][-1]['content'] == 'New concurrent tail.'


def test_crash_boundaries_cold_attach_exact_recall(tmp_path):
    from bench.compaction_durable import BOUNDARIES, FACTS, run_case
    for boundary in BOUNDARIES:
        result = run_case(tmp_path / boundary, boundary)
        assert result['boundary_reached']
        assert result['history_tool_exact'] == len(FACTS), result
        assert result['summary_exact'] == 0, result
        if boundary in ('after_transcript_replace', 'after_success'):
            assert result['residual_present']
            assert result['archive_state'] == 'verified'


@pytest.mark.parametrize('damage', ['archive', 'transcript', 'missing_transcript'])
def test_pending_commit_recovers_corrupt_target_and_archive(tmp_path, damage):
    import json
    from pathlib import Path
    import subprocess
    import sys
    from bench.compaction_durable import SID, inspect_cold
    result = subprocess.run([sys.executable, '-m', 'bench.compaction_durable', '--child', 'crash',
                             '--state', str(tmp_path), '--boundary', 'after_transcript_replace'], timeout=30)
    assert result.returncode == 71
    archive = tmp_path / 'transcripts' / (SID + '.archive.json')
    if damage == 'archive':
        document = json.loads(archive.read_text())
        segment = Path(str(archive) + '.segments') / (document['head'] + '.json')
        document = json.loads(segment.read_text())
        document['messages'][0]['content'] = 'tampered'
        segment.write_text(json.dumps(document))
    elif damage == 'transcript':
        (tmp_path / 'transcripts' / (SID + '.json')).write_text('{bad')
    else:
        (tmp_path / 'transcripts' / (SID + '.json')).unlink()
    cold = inspect_cold(str(tmp_path))
    assert not cold['residual_present']
    assert cold['archive_state'] == 'unavailable'
    assert cold['history_tool_exact'] == 3
    # Recovery is stable on repeated cold loads.
    assert inspect_cold(str(tmp_path))['history_tool_exact'] == 3


def test_archive_readback_mismatch_aborts(tmp_path, monkeypatch):
    import json
    import os
    session = fat_session(tmp_path, monkeypatch)
    monkeypatch.setenv('HARNESS_COMPACTION_RESIDUAL', 'catalog')
    original = copy.deepcopy(session._history)
    replace = os.replace

    def tampered(src, dst):
        replace(src, dst)
        if str(dst).endswith('.archive.json'):
            with open(dst, 'w') as handle:
                json.dump({'version': 1, 'session_id': 'default', 'messages': []}, handle)
    monkeypatch.setattr(os, 'replace', tampered)
    events = list(session._maybe_compact_history(force=True))
    assert events[-1].data['aborted']
    assert session._history == original
    assert load_transcript(str(tmp_path), 'default')['history'] == original[1:]


def test_retry_after_archive_crash_does_not_duplicate_rows(tmp_path):
    from bench.compaction_durable import SID, run_case
    from harness.compaction_archive import load_compaction_archive_messages
    first = run_case(tmp_path, 'after_archive')
    assert first['history_tool_exact'] == 3
    assert load_compaction_archive_messages(str(tmp_path), SID) == []
    retry = run_case(tmp_path, 'after_success')
    assert retry['archive_lookup_exact'] == 3
    rows = load_compaction_archive_messages(str(tmp_path), SID)
    assert len(rows) == 18
    from bench.compaction_durable import FACTS
    for fact in FACTS.values():
        assert sum(fact in str(row.get('content', '')) for row in rows) == 1


@pytest.mark.parametrize('publication', ['segment', 'transcript'])
def test_fsync_failure_retains_original_transcript(tmp_path, monkeypatch, publication):
    import os
    import stat
    from harness import compaction_archive as archive
    from harness.history_compaction_journal import commit_compacted_transcript
    from harness.sessions import save_transcript
    state, sid = str(tmp_path), 'fsync_failure'
    source = {'history': [{'role': 'user', 'content': 'exact original'}]}
    target = {'history': [{'role': 'assistant', 'content': 'summary'}]}
    save_transcript(state, sid, source)
    real_fsync, real_mkstemp = os.fsync, archive.tempfile.mkstemp
    selected = set()

    def tracked(*args, **kwargs):
        fd, path = real_mkstemp(*args, **kwargs)
        is_segment = str(kwargs.get('dir', '')).endswith('.segments')
        is_transcript = kwargs.get('prefix') == sid + '.json.'
        if (publication == 'segment' and is_segment) or (publication == 'transcript' and is_transcript):
            selected.add(fd)
        return fd, path

    def fail_once(fd):
        if fd in selected:
            selected.remove(fd)
            if stat.S_ISREG(os.fstat(fd).st_mode):
                raise OSError('publication fsync failed')
        return real_fsync(fd)

    with monkeypatch.context() as patch:
        patch.setattr(archive.tempfile, 'mkstemp', tracked)
        patch.setattr(os, 'fsync', fail_once)
        with pytest.raises(OSError):
            commit_compacted_transcript(state, sid, source, target, source['history'])
    assert load_transcript(state, sid) == source
    assert load_transcript(state, sid) == source
    assert archive.load_compaction_archive_messages(state, sid) == []
