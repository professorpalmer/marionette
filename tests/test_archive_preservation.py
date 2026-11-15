import json
import sqlite3
import pytest
from harness import chat_archive as a
from harness.sessions import save_transcript, load_transcript


def seed(tmp_path, count=3):
    raw = {'history': [{'role': 'user', 'content': 'quasar fact %s' % i} for i in range(count)],
           'display': [{'kind': 'tool', 'text': 'native display'}], 'job_ids': ['job-42']}
    raw['history'].append({'role': 'assistant', 'content': '', 'tool_calls': [{'id': 'native', 'function': {'name': 'run_command', 'arguments': '{}'}}]})
    raw['history'][-1]['reasoning_content'] = 'preserved reasoning'
    raw['history'][-1]['tool_calls'][0]['type'] = 'function'
    raw['history'][-1]['tool_calls'][0]['thought_signature'] = 'opaque-signature'
    raw['history'].append({'role': 'tool', 'tool_call_id': 'native', 'content': '',
                           'name': 'run_command', 'is_error': False})
    raw['display'][0]['data'] = {'tool_call_id': 'native', 'job_id': 'job-42'}
    save_transcript(str(tmp_path), 'cold', raw)
    return raw, [{'id': 'cold', 'archived': True}]


def test_structured_roundtrip(tmp_path):
    raw, rows = seed(tmp_path)
    a.ingest_all(str(tmp_path), sessions=rows)
    assert load_transcript(str(tmp_path), 'cold') == raw
    assert a.prune_ingested_transcripts(str(tmp_path), rows)['pruned'] == 1
    assert a.ingest_all(str(tmp_path), sessions=rows)['skipped_unchanged'] == 1
    assert a.restore_pruned_transcript(str(tmp_path), 'cold')
    assert load_transcript(str(tmp_path), 'cold') == raw


def test_long_roundtrip(tmp_path):
    raw, rows = seed(tmp_path, 2101)
    a.ingest_all(str(tmp_path), sessions=rows)
    assert len(a.read_archived_chat(str(tmp_path), 'marionette:cold')['messages']) == 200
    assert a.prune_ingested_transcripts(str(tmp_path), rows)['pruned'] == 1
    assert a.restore_pruned_transcript(str(tmp_path), 'cold')
    assert load_transcript(str(tmp_path), 'cold') == raw


def test_structured_change_blocks_prune(tmp_path):
    raw, rows = seed(tmp_path)
    a.ingest_all(str(tmp_path), sessions=rows)
    raw['display'].append({'text': 'new sole copy'})
    save_transcript(str(tmp_path), 'cold', raw)
    assert a.prune_ingested_transcripts(str(tmp_path), rows)['pruned'] == 0
    assert load_transcript(str(tmp_path), 'cold') == raw


def test_corrupt_backup_blocks_prune(tmp_path):
    raw, rows = seed(tmp_path)
    a.ingest_all(str(tmp_path), sessions=rows)
    payload = a.read_archived_chat(str(tmp_path), 'marionette:cold')
    from pathlib import Path
    Path(payload['backup_path']).write_text('corrupt')
    assert a.prune_ingested_transcripts(str(tmp_path), rows)['pruned'] == 0
    assert load_transcript(str(tmp_path), 'cold') == raw


def test_backup_failure_keeps_hot(tmp_path, monkeypatch):
    raw, rows = seed(tmp_path)
    def fail(*args, **kwargs):
        raise OSError('simulated disk failure')
    monkeypatch.setattr(a, '_write_backup', fail)
    with pytest.raises(OSError):
        a.ingest_all(str(tmp_path), sessions=rows)
    assert a.prune_ingested_transcripts(str(tmp_path), rows)['pruned'] == 0
    assert load_transcript(str(tmp_path), 'cold') == raw


@pytest.mark.parametrize('target', ['payload', 'backup', 'fingerprint'])
def test_conflicting_copy_blocks_prune(tmp_path, target):
    raw, rows = seed(tmp_path)
    a.ingest_all(str(tmp_path), sessions=rows)
    con = sqlite3.connect(str(a.archive_db_path(str(tmp_path))))
    if target == 'payload':
        con.execute("UPDATE raw_payloads SET payload='{}'")
    elif target == 'fingerprint':
        con.execute("UPDATE chats SET content_fp='conflicting'")
    else:
        from pathlib import Path
        path = con.execute('SELECT backup_path FROM raw_payloads').fetchone()[0]
        Path(path).write_text('{}')
    con.commit()
    con.close()
    assert a.prune_ingested_transcripts(str(tmp_path), rows)['pruned'] == 0
    assert load_transcript(str(tmp_path), 'cold') == raw


def test_interrupted_prune_keeps_copy_and_retry(tmp_path, monkeypatch):
    import harness.compaction_archive as ca
    raw, rows = seed(tmp_path)
    a.ingest_all(str(tmp_path), sessions=rows)
    original = ca.os.replace
    def fail(*args):
        raise OSError('simulated replace interruption')
    with monkeypatch.context() as m:
        m.setattr(ca.os, 'replace', fail)
        assert a.prune_ingested_transcripts(str(tmp_path), rows)['pruned'] == 0
    assert load_transcript(str(tmp_path), 'cold') == raw
    assert a.prune_ingested_transcripts(str(tmp_path), rows)['pruned'] == 1
    assert a.prune_ingested_transcripts(str(tmp_path), rows)['pruned'] == 0
    assert a.restore_pruned_transcript(str(tmp_path), 'cold')
    assert load_transcript(str(tmp_path), 'cold') == raw


def test_missing_corrupt_and_active_hot_not_overwritten(tmp_path):
    raw, rows = seed(tmp_path)
    a.ingest_all(str(tmp_path), sessions=rows)
    assert a.prune_ingested_transcripts(str(tmp_path), [{'id': 'cold', 'archived': False}])['pruned'] == 0
    assert not a.restore_pruned_transcript(str(tmp_path), 'cold')
    path = tmp_path / 'transcripts/cold.json'
    path.write_text('{broken')
    assert a.ingest_all(str(tmp_path), sessions=rows)['errors'] == 1
    assert a.prune_ingested_transcripts(str(tmp_path), rows)['pruned'] == 0
    assert path.read_text() == '{broken'
    assert not a.restore_pruned_transcript(str(tmp_path), 'cold')
    path.unlink()
    assert a.ingest_all(str(tmp_path), sessions=rows)['errors'] == 1
    assert a.restore_pruned_transcript(str(tmp_path), 'cold')
    assert load_transcript(str(tmp_path), 'cold') == raw


def test_legacy_restore_uncapped_but_no_invented_native_state(tmp_path):
    raw, rows = seed(tmp_path, 2101)
    a.ingest_all(str(tmp_path), sessions=rows)
    con = sqlite3.connect(str(a.archive_db_path(str(tmp_path))))
    con.execute('DROP TABLE raw_payloads')
    con.commit()
    con.close()
    assert a.prune_ingested_transcripts(str(tmp_path), rows)['pruned'] == 0
    (tmp_path / 'transcripts/cold.json').write_text('{"pruned": true}')
    assert a.restore_pruned_transcript(str(tmp_path), 'cold')
    restored = load_transcript(str(tmp_path), 'cold')
    assert len(restored) == 2101
    assert isinstance(restored, list)
    assert all('tool_calls' not in m for m in restored)


def test_real_catalog_compaction_archive_restore_peek(tmp_path, monkeypatch):
    from harness.config import HarnessConfig
    from harness.conversation import ConversationalSession
    from harness.pilot import PilotAction
    from harness.compaction_archive import load_compaction_archive_page
    monkeypatch.setenv('HARNESS_COMPACTION_RESIDUAL', 'catalog')
    monkeypatch.setattr('harness.compaction_mixin.MIN_COMPACTABLE_TOKENS', 0)
    state = str(tmp_path)
    session = ConversationalSession(HarnessConfig(max_context_tokens=4000, state_dir=state))
    session.harness_session_id = 'cold'
    class NoModel:
        def chat(self, *args, **kwargs):
            raise AssertionError('catalog must remain deterministic')
    session.pilot = NoModel()
    session._history = [{'role': 'system', 'content': 'sys'}]
    for i in range(30):
        session._history.extend([
            {'role': 'user', 'content': 'Decision: preserve quasar-fact-9z at harness/archive.py ' + str(i) + 'x' * 500},
            {'role': 'assistant', 'content': '', 'tool_calls': [{'id': 'call%s' % i, 'type': 'function', 'function': {'name': 'run_command', 'arguments': '{}'}}]},
            {'role': 'tool', 'tool_call_id': 'call%s' % i, 'content': 'native result ' + 'y' * 500},
            {'role': 'assistant', 'content': 'checked ' + 'z' * 500}])
    events = list(session._maybe_compact_history(force=True))
    assert any(e.kind == 'compaction' and not e.data.get('aborted') for e in events)
    assert 'Handle catalog' in str(session._history)
    archived, total = load_compaction_archive_page(state, 'cold', limit=400)
    assert total and any('tool_calls' in m for m in archived)
    raw = session.export_transcript_data()
    raw['display'] = [{'kind': 'tool', 'text': 'distinct display preserved'}]
    save_transcript(state, 'cold', raw)
    rows = [{'id': 'cold', 'archived': True}]
    assert a.ingest_all(state, sessions=rows)['ingested'] == 1
    assert a.prune_ingested_transcripts(state, rows)['pruned'] == 1
    assert a.search_archive(state, 'quasar-fact-9z')
    assert a.read_archived_chat(state, 'marionette:cold')
    # Only disposable fixture sidecars are removed to prove recovery from the vault.
    for path in (tmp_path / 'transcripts').glob('cold.archive.json.segments/*.json'):
        path.unlink()
    (tmp_path / 'transcripts/cold.archive.json').unlink()
    assert a.restore_pruned_transcript(state, 'cold')
    assert load_transcript(state, 'cold') == raw
    assert load_compaction_archive_page(state, 'cold', limit=400) == (archived, total)
    cold = ConversationalSession(HarnessConfig(max_context_tokens=4000, state_dir=state))
    cold.harness_session_id = 'cold'
    cold._history = raw['history']
    ok, status, text = cold._do_peek_history(PilotAction(kind='peek_history', arguments={'offset': 0, 'limit': 20}))
    assert ok and 'quasar-fact-9z' in text


def test_native_roundtrip_without_reingest(tmp_path):
    raw, rows = seed(tmp_path)
    a.ingest_all(str(tmp_path), sessions=rows)
    assert a.prune_ingested_transcripts(str(tmp_path), rows)['pruned'] == 1
    assert a.restore_pruned_transcript(str(tmp_path), 'cold')
    assert load_transcript(str(tmp_path), 'cold') == raw


def test_failed_ingest_transaction_keeps_prior_and_hot(tmp_path, monkeypatch):
    raw, rows = seed(tmp_path)
    a.ingest_all(str(tmp_path), sessions=rows)
    prior = a.read_archived_chat(str(tmp_path), 'marionette:cold')
    raw['history'].append({'role': 'user', 'content': 'new sole hot fact'})
    save_transcript(str(tmp_path), 'cold', raw)
    def fail(*args, **kwargs):
        raise sqlite3.OperationalError('simulated ingest transaction interruption')
    with monkeypatch.context() as m:
        m.setattr(a, '_upsert_chat', fail)
        with pytest.raises(sqlite3.OperationalError):
            a.ingest_all(str(tmp_path), sessions=rows)
    assert a.read_archived_chat(str(tmp_path), 'marionette:cold') == prior
    assert load_transcript(str(tmp_path), 'cold') == raw
    assert a.prune_ingested_transcripts(str(tmp_path), rows)['pruned'] == 0
    assert a.ingest_all(str(tmp_path), sessions=rows)['ingested'] == 1


def test_api_handoff_keeps_failed_restore_archived(tmp_path):
    from types import SimpleNamespace
    from pathlib import Path
    from harness.api import sessions as api
    raw, rows = seed(tmp_path)
    a.ingest_all(str(tmp_path), sessions=rows)
    a.prune_ingested_transcripts(str(tmp_path), rows)
    changes = []
    svc = SimpleNamespace(parse_bool=bool, sessions_state_dir=lambda: str(tmp_path), sessions=SimpleNamespace(archive=lambda sid, flag: changes.append(flag)))
    payload = a.read_archived_chat(str(tmp_path), 'marionette:cold')
    backup = Path(payload['backup_path'])
    saved = backup.read_bytes()
    backup.write_text('corrupt')
    code, _ = api.post_sessions_archive({'id': 'cold', 'archived': False}, svc)
    assert code == 409 and changes == []
    backup.write_bytes(saved)
    code, _ = api.post_sessions_archive({'id': 'cold', 'archived': False}, svc)
    assert code == 200 and changes == [False]
    assert load_transcript(str(tmp_path), 'cold') == raw


def test_corrupt_database_retains_independent_raw_backup(tmp_path):
    from pathlib import Path
    raw, rows = seed(tmp_path)
    a.ingest_all(str(tmp_path), sessions=rows)
    db = a.archive_db_path(str(tmp_path))
    con = sqlite3.connect(str(db))
    backup = Path(con.execute('SELECT backup_path FROM raw_payloads').fetchone()[0])
    con.close()
    assert a.prune_ingested_transcripts(str(tmp_path), rows)['pruned'] == 1
    db.write_bytes(b'corrupt database')
    assert not a.restore_pruned_transcript(str(tmp_path), 'cold')
    assert json.loads(backup.read_text())['transcript'] == raw
    assert json.loads((tmp_path / 'transcripts/cold.json').read_text())['pruned'] is True


@pytest.mark.parametrize("damage", ["backup", "payload", "database"])
def test_missing_hot_failed_recovery_stays_archived(tmp_path, damage):
    from pathlib import Path
    from types import SimpleNamespace
    from harness.api import sessions as api
    _, rows = seed(tmp_path)
    a.ingest_all(str(tmp_path), sessions=rows)
    assert a.prune_ingested_transcripts(str(tmp_path), rows)['pruned'] == 1
    (tmp_path / 'transcripts/cold.json').unlink()
    db = a.archive_db_path(str(tmp_path))
    with sqlite3.connect(str(db)) as con:
        if damage == 'backup':
            Path(con.execute('SELECT backup_path FROM raw_payloads').fetchone()[0]).write_text('{}')
        elif damage == 'payload':
            con.execute("UPDATE raw_payloads SET payload='{}'")
    if damage == 'database':
        db.write_bytes(b'corrupt database')
    changes = []
    svc = SimpleNamespace(parse_bool=bool, sessions_state_dir=lambda: str(tmp_path),
                          sessions=SimpleNamespace(archive=lambda sid, flag: changes.append(flag)))
    code, _ = api.post_sessions_archive({'id': 'cold', 'archived': False}, svc)
    assert code == 409
    assert changes == []
    assert not (tmp_path / 'transcripts/cold.json').exists()


@pytest.mark.parametrize('existing_db', [False, True])
def test_never_saved_session_can_unarchive(tmp_path, existing_db):
    from types import SimpleNamespace
    from harness.api import sessions as api
    if existing_db:
        _, rows = seed(tmp_path)
        a.ingest_all(str(tmp_path), sessions=rows)
    changes = []
    svc = SimpleNamespace(parse_bool=bool, sessions_state_dir=lambda: str(tmp_path),
                          sessions=SimpleNamespace(archive=lambda sid, flag: changes.append(flag)))
    code, _ = api.post_sessions_archive({'id': 'never-saved', 'archived': False}, svc)
    assert code == 200 and changes == [False]


@pytest.mark.parametrize('malformed', [None, 42, 'text', {}, {'history': 'text'},
                                      {'history': {}}, {'history': 7}, [None], {'history': [7]}])
def test_malformed_json_cannot_replace_archive(tmp_path, malformed):
    _, rows = seed(tmp_path)
    a.ingest_all(str(tmp_path), sessions=rows)
    prior = a.read_archived_chat(str(tmp_path), 'marionette:cold')
    path = tmp_path / 'transcripts/cold.json'
    path.write_text(json.dumps(malformed))
    assert a.ingest_all(str(tmp_path), sessions=rows)['errors'] == 1
    assert a.read_archived_chat(str(tmp_path), 'marionette:cold') == prior
    assert a.prune_ingested_transcripts(str(tmp_path), rows)['pruned'] == 0
    assert json.loads(path.read_text()) == malformed


@pytest.mark.parametrize('raw', [[], {'history': [], 'display': [], 'job_ids': []}])
def test_saved_empty_transcript_roundtrip(tmp_path, raw):
    save_transcript(str(tmp_path), 'cold', raw)
    rows = [{'id': 'cold', 'archived': True}]
    assert a.ingest_all(str(tmp_path), sessions=rows)['ingested'] == 1
    assert a.prune_ingested_transcripts(str(tmp_path), rows)['pruned'] == 1
    assert a.restore_pruned_transcript(str(tmp_path), 'cold')
    assert load_transcript(str(tmp_path), 'cold') == raw


def test_session_load_does_not_wait_behind_archive_verify(tmp_path, monkeypatch):
    # A user session switch saves and loads transcripts under the native
    # publication lock. The boot ingest must not hold that lock while it
    # verifies the vault copy of an archived session.
    import threading
    raw, rows = seed(tmp_path)
    a.ingest_all(str(tmp_path), sessions=rows)
    save_transcript(str(tmp_path), 'live', [{'role': 'user', 'content': 'live session'}])
    entered, release = threading.Event(), threading.Event()
    original = a._content_fp
    def pause(*args):
        if threading.current_thread().name == 'boot-ingest' and not entered.is_set():
            entered.set()
            assert release.wait(5)
        return original(*args)
    monkeypatch.setattr(a, '_content_fp', pause)
    ingest = threading.Thread(target=a.ingest_all, args=(str(tmp_path),),
                              kwargs={'sessions': rows}, name='boot-ingest')
    ingest.start()
    try:
        assert entered.wait(5)
        loaded = []
        switch = threading.Thread(target=lambda: loaded.append(load_transcript(str(tmp_path), 'live')))
        switch.start()
        switch.join(2)
        assert loaded == [[{'role': 'user', 'content': 'live session'}]]
    finally:
        release.set()
        ingest.join(5)
    assert load_transcript(str(tmp_path), 'cold') == raw


def test_boot_moves_archived_into_vault_then_skips_stubs(tmp_path, monkeypatch):
    raw, rows = seed(tmp_path)
    monkeypatch.delenv('PYTEST_CURRENT_TEST', raising=False)
    a.maybe_boot_ingest(str(tmp_path), rows)
    path = tmp_path / 'transcripts/cold.json'
    assert json.loads(path.read_text())['pruned'] is True
    assert a.search_archive(str(tmp_path), 'quasar')
    def no_full_read(*args):
        raise AssertionError('a stub needs no bundle read')
    with monkeypatch.context() as m:
        m.setattr(a, '_bundle', no_full_read)
        report = a.ingest_all(str(tmp_path), sessions=rows, prune=True)
    assert (report['skipped_unchanged'], report['pruned'], report['errors']) == (1, 0, 0)
    # The hover prefetch reads the vault copy and leaves the stub in place.
    assert a.pruned_transcript_view(str(tmp_path), 'cold') == raw
    assert json.loads(path.read_text())['pruned'] is True
    # A runner never attaches over a stub: it gets the full session back.
    from harness.sessions import load_runner_transcript
    assert load_runner_transcript(str(tmp_path), 'cold') == raw
    assert load_transcript(str(tmp_path), 'cold') == raw


def test_boot_keeps_hot_copy_that_differs_from_vault(tmp_path, monkeypatch):
    raw, rows = seed(tmp_path)
    a.ingest_all(str(tmp_path), sessions=rows)
    raw['history'].append({'role': 'user', 'content': 'newer hot fact'})
    save_transcript(str(tmp_path), 'cold', raw)
    real_digest = a._vault_digest
    def stale_digest(*args):
        # The vault changes between verification and the stub write.
        return 'changed-' + str(real_digest(*args))
    monkeypatch.setattr(a, '_vault_digest', stale_digest)
    report = a.ingest_all(str(tmp_path), sessions=rows, prune=True)
    assert report['ingested'] == 1 and report['pruned'] == 0
    assert load_transcript(str(tmp_path), 'cold') == raw
