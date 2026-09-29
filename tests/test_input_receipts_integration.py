"""Production API, native publication, and disposable archive evidence."""
import base64
import copy
import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from harness.api.session_control import SessionControlServices, post_session_queue, post_session_steer, get_session_queue
from harness.api.files import get_image
from harness.config import HarnessConfig
from harness.conversation import ConversationalSession
from harness.input_receipts import InputReceiptStore, InputReceiptError, session_input_store
from harness.sessions import save_transcript, load_transcript


@pytest.fixture
def session(tmp_path, monkeypatch):
    monkeypatch.setattr('harness.conversation.prov.build_pilot', lambda *_: SimpleNamespace())
    for name in ('SkillStore', 'RuleStore'):
        monkeypatch.setattr('harness.conversation.' + name, lambda *_a, **_k: SimpleNamespace(list=lambda *_: []))
    monkeypatch.setattr('harness.conversation.MemoryStore', lambda *_a, **_k: SimpleNamespace(render_block=lambda: ''))
    monkeypatch.setattr('harness.conversation.WikiClient', lambda *_a, **_k: SimpleNamespace(configured=False))
    monkeypatch.setattr('harness.plugin_registry.list_enabled_plugin_skills', lambda *_a, **_k: [])
    monkeypatch.setattr('harness.browser_auth.ensure_shared_browser_env', lambda: {})
    s = ConversationalSession(HarnessConfig(state_dir=str(tmp_path), repo='', swarm_adapter='demo'))
    s.harness_session_id = 'receipts-test'
    s.bind_prompt_queue(str(tmp_path), s.harness_session_id)
    return s


def services(session):
    upload_dir = Path(session.state_dir) / 'uploads'
    upload_dir.mkdir(exist_ok=True)
    return SessionControlServices(cfg=session.config, get_pilot=lambda: session,
        get_runners=lambda: {session.harness_session_id: session}, gate_active_pilot_ready=lambda: None,
        stash_put=lambda *_: '', save_active_transcript=lambda: None,
        upload_dir=str(upload_dir), diag=lambda *_a, **_k: None)


def seed_history(session):
    # Complete tool pair and opaque data must survive strict receipt publication.
    session._history.extend([
        {'role': 'user', 'content': 'earlier input'},
        {'role': 'assistant', 'content': '', 'tool_calls': [{'id': 'tool-1', 'type': 'function',
            'function': {'name': 'read_file', 'arguments': '{}'}, 'thought_signature': 'opaque'}],
         'reasoning_content': 'opaque reasoning'},
        {'role': 'tool', 'tool_call_id': 'tool-1', 'content': 'tool result'},
    ])
    raw = session.export_transcript_data()
    save_transcript(session.state_dir, session.harness_session_id, raw)
    return copy.deepcopy(raw['history'])


def test_api_preserves_original_and_distinct_equal_input_ids(session):
    svc = services(session)
    original = '  Duplicate exact text\n'
    responses = [post_session_steer({'text': original}, svc) for _ in range(2)]
    assert all(code == 200 for code, _ in responses)
    receipts = session.input_receipts()
    assert [r['original_text'] for r in receipts] == [original, original]
    assert len({r['id'] for r in receipts}) == 2
    assert [a.id for a in session._session_actions] == [r['id'] for r in receipts]
    before = seed_history(session)
    events = list(session._check_and_inject_steer())
    assert len(events) == 2
    assert all(r['status'] == 'injected' for r in session.input_receipts())
    disk = load_transcript(session.state_dir, session.harness_session_id)
    assert disk['history'][:len(before)] == before
    assert [r['input_id'] for r in disk['history'][len(before):]] == [r['id'] for r in receipts]


def test_retry_key_deduplicates_projection_and_conflict_rejects(session):
    svc = services(session)
    body = {'text': ' exact \n', 'retry_key': 'same'}
    first = post_session_queue(body, svc)
    second = post_session_queue(body, svc)
    assert first[0] == second[0] == 200
    assert first[1]['item']['id'] == second[1]['item']['id']
    assert len(session.list_prompts()) == 1
    code, error = post_session_queue({**body, 'text': 'different'}, svc)
    assert code >= 400 and error['code'] == 'input_retry_conflict'
    assert len(session.input_receipts()) == 1


def test_stop_retains_original_and_blocks_late_delivery(session):
    svc = services(session)
    post_session_steer({'text': ' stop original \n'}, svc)
    actions = session._drain_steer_actions()
    session._interrupt_requested = True
    session._cancel.set()
    assert session._publish_input_actions(actions, ['converted']) is False
    receipt = session.input_receipts()[0]
    assert receipt['status'] == 'dropped'
    assert receipt['original_text'] == ' stop original \n'
    assert session.export_history() == []


def test_pop_gap_is_retained_and_cold_input_is_not_runnable(session):
    original = 'pop gap'
    queued = session.enqueue_prompt(original)
    item = session._pop_next_prompt()
    assert item['input_id'] == queued['id']
    assert session.list_prompts() == []
    cold = InputReceiptStore(session.state_dir, session.harness_session_id)
    assert cold.list()[0]['status'] == 'uncertain'
    assert cold.list()[0]['original_text'] == original
    with pytest.raises(InputReceiptError):
        cold.prepare_delivery(item['input_id'], handoff_token=item['handoff_token'])


def test_exact_ids_not_equal_text_reconcile_after_transcript_replace(session, monkeypatch):
    svc = services(session)
    post_session_steer({'text': 'same'}, svc)
    post_session_steer({'text': 'same'}, svc)
    first, second = list(session._session_actions)
    receipts = session_input_store(session)
    receipts.transition(first.id, 'delivering')
    receipts.transition(second.id, 'delivering')
    raw = {'history': [{'role': 'user', 'content': 'same', 'input_id': first.id}], 'display': [], 'job_ids': []}
    def fail(_document):
        raise InputReceiptError('injected_fault', 'after transcript replacement')
    with monkeypatch.context() as patcher:
        patcher.setattr(receipts, '_write', fail)
        with pytest.raises(InputReceiptError):
            receipts.publish_injected([first.id], raw)
    cold = InputReceiptStore(session.state_dir, session.harness_session_id)
    rows = {r['id']: r for r in cold.list()}
    assert rows[first.id]['status'] == 'injected'
    assert rows[second.id]['status'] == 'uncertain'


def test_failure_before_admission_has_no_runnable_projection(session, monkeypatch):
    svc = services(session)
    def fail(*_args, **_kwargs):
        raise OSError('before admission commit')
    with monkeypatch.context() as patcher:
        patcher.setattr('harness.compaction_archive._atomic_write_json', fail)
        code, error = post_session_steer({'text': 'keep my draft'}, svc)
        assert code >= 400 and error['code'] == 'input_commit_uncertain'
    assert len(session._session_actions) == 0
    assert session.input_receipts() == []


def test_native_bundle_retains_image_document_and_exact_history(session):
    from harness import chat_archive
    svc = services(session)
    image = Path(svc.upload_dir) / 'image.png'
    document = Path(svc.upload_dir) / 'document.txt'
    image_bytes = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl6nAAAAABJRU5ErkJggg==')
    image.write_bytes(image_bytes)
    document.write_bytes(b'original document\x00bytes\n')
    original = ' exact original with file '
    code, result = post_session_queue({'text': original, 'images': [str(image)],
                    'documents': [{'path': str(document), 'name': 'document.txt'}]}, svc)
    assert code == 200, result
    seed_history(session)
    raw = load_transcript(session.state_dir, session.harness_session_id)
    receipts = session_input_store(session)
    row = receipts.list()[0]
    expected = {a['ref']: receipts.attachment(a['ref']) for a in row['attachments']}
    catalog = [{'id': session.harness_session_id, 'archived': True}]
    assert chat_archive.ingest_all(session.state_dir, sessions=catalog)['ingested'] == 1
    assert chat_archive.prune_ingested_transcripts(session.state_dir, catalog)['pruned'] == 1
    image.unlink()
    document.unlink()
    receipts.path.unlink()  # Only disposable fixture sidecars are deleted.
    assert chat_archive.restore_pruned_transcript(session.state_dir, session.harness_session_id)
    assert load_transcript(session.state_dir, session.harness_session_id) == raw
    cold = InputReceiptStore(session.state_dir, session.harness_session_id)
    assert cold.list()[0]['original_text'] == original
    assert cold.list()[0]['held']
    assert {ref: cold.attachment(ref) for ref in expected} == expected
    ref = next(a['ref'] for a in row['attachments'] if a['kind'] == 'image')
    status, data, _ = get_image(ref, svc.upload_dir, session=session)
    assert status == 200 and data == image_bytes
    assert hashlib.sha256(data).hexdigest() == next(a['sha256'] for a in row['attachments'] if a['ref'] == ref)


def test_new_admission_blocks_stale_archive_prune(session):
    from harness import chat_archive
    seed_history(session)
    session.enqueue_prompt('before')
    catalog = [{'id': session.harness_session_id, 'archived': True}]
    chat_archive.ingest_all(session.state_dir, sessions=catalog)
    session.enqueue_prompt('after')
    assert chat_archive.prune_ingested_transcripts(session.state_dir, catalog)['pruned'] == 0


def test_retained_mention_cannot_read_unowned_path(session):
    svc = services(session)
    uploaded = Path(svc.upload_dir) / 'note.txt'
    uploaded.write_bytes(b'exact upload')
    outside = Path(session.state_dir) / 'private.txt'
    outside.write_bytes(b'not an upload')
    original = '@"' + str(uploaded) + '" @"' + str(outside) + '"'
    code, result = post_session_queue({'text': original}, svc)
    assert code == 200, result
    row = session.input_receipts()[0]
    assert len(row['attachments']) == 1
    assert session_input_store(session).attachment(row['attachments'][0]['ref']) == b'exact upload'


def test_native_revision_conflict_does_not_overwrite_new_history(session):
    store = session_input_store(session)
    row = store.admit('new user')
    store.prepare_delivery(row['id'])
    competing = {'history': [{'role': 'assistant', 'content': 'newer writer'}]}
    save_transcript(session.state_dir, session.harness_session_id, competing)
    with pytest.raises(InputReceiptError):
        store.publish_injected([row['id']], {'history': [{'role': 'user', 'content': 'new user', 'input_id': row['id']}]})
    assert load_transcript(session.state_dir, session.harness_session_id) == competing


def test_steer_transcription_retains_original_pixels_and_text(session, monkeypatch):
    svc = services(session)
    path = Path(svc.upload_dir) / 'image.png'
    pixels = b'original image bytes for a synthetic transcription'
    path.write_bytes(pixels)
    monkeypatch.setattr('harness.vision.session_supports_native_images', lambda *_: False)
    monkeypatch.setattr('harness.vision.transcribe_images', lambda paths: [SimpleNamespace(text='converted words', error=None)])
    code, reply = post_session_steer({'text': '  original words\n', 'images': [str(path)]}, svc)
    assert code == 200, reply
    assert len(session.input_receipts()) == 1
    receipt = session.input_receipts()[0]
    assert receipt['original_text'] == '  original words\n'
    path.unlink()
    assert session_input_store(session).attachment(receipt['attachments'][0]['ref']) == pixels
    assert 'converted words' in list(session._session_actions)[0].text
    list(session._check_and_inject_steer())
    assert session.input_receipts()[0]['status'] == 'injected'


def test_handoff_token_is_one_use(session):
    svc = services(session)
    code, result = post_session_queue({'text': 'handoff'}, svc)
    assert code == 200
    input_id = result['item']['id']
    code, result = post_session_queue({'handoff': input_id}, svc)
    assert code == 200
    store = session_input_store(session)
    token = result['item']['handoff_token']
    store.prepare_delivery(input_id, handoff_token=token)
    with pytest.raises(InputReceiptError):
        store.prepare_delivery(input_id, handoff_token=token)
    assert session.list_prompts() == []


def test_unwritable_storage_returns_local_action_error(session):
    svc = services(session)
    (Path(session.state_dir) / 'transcripts').write_text('not a directory')
    code, result = post_session_steer({'text': 'keep draft'}, svc)
    assert code >= 400 and result['code'] == 'input_storage_unavailable'
    assert len(session._session_actions) == 0


def test_stop_admission_barrier(session, monkeypatch):
    svc = services(session)
    store = session_input_store(session)
    session._busy.acquire()
    entered, release = threading.Event(), threading.Event()
    original = store._write
    def pause(data):
        if any(row['status'] == 'accepted' for row in data['inputs']) and not entered.is_set():
            entered.set()
            assert release.wait(3)
        original(data)
    monkeypatch.setattr(store, '_write', pause)
    with ThreadPoolExecutor(max_workers=2) as pool:
        admission = pool.submit(post_session_steer, {'text': 'racing original'}, svc)
        assert entered.wait(3)
        stopping = pool.submit(session.interrupt)
        assert session._cancel.wait(3)
        release.set()
        assert admission.result(timeout=3)[0] == 200
        stopping.result(timeout=3)
    assert len(session._session_actions) == 0
    assert session.input_receipts()[0]['status'] == 'dropped'
    assert session.input_receipts()[0]['original_text'] == 'racing original'


def test_drain_stop_barrier(session, monkeypatch):
    svc = services(session)
    post_session_steer({'text': 'drain race'}, svc)
    drained, release = threading.Event(), threading.Event()
    original = session._publish_input_actions
    def pause(actions, contents, **kwargs):
        drained.set()
        assert release.wait(3)
        return original(actions, contents, **kwargs)
    monkeypatch.setattr(session, '_publish_input_actions', pause)
    with ThreadPoolExecutor(max_workers=2) as pool:
        injecting = pool.submit(lambda: list(session._check_and_inject_steer()))
        assert drained.wait(3)
        session.interrupt()
        release.set()
        assert injecting.result(timeout=3) == []
    assert session.input_receipts()[0]['status'] == 'dropped'
    assert session.export_history() == []


def test_archive_admission_barrier_blocks_stale_prune(session, monkeypatch):
    from harness import chat_archive
    seed_history(session)
    session.enqueue_prompt('before archive')
    catalog = [{'id': session.harness_session_id, 'archived': True}]
    entered, release, admission_started = threading.Event(), threading.Event(), threading.Event()
    original = chat_archive._bundle
    def pause(*args):
        result = original(*args)
        if not entered.is_set():
            entered.set()
            assert release.wait(3)
        return result
    monkeypatch.setattr(chat_archive, '_bundle', pause)
    def admit():
        admission_started.set()
        return session.enqueue_prompt('during archive')
    with ThreadPoolExecutor(max_workers=2) as pool:
        archive = pool.submit(chat_archive.ingest_all, session.state_dir, sessions=catalog)
        assert entered.wait(3)
        admission = pool.submit(admit)
        assert admission_started.wait(3)
        assert not admission.done()
        release.set()
        assert archive.result(timeout=3)['ingested'] == 1
        assert admission.result(timeout=3)['id']
    assert chat_archive.prune_ingested_transcripts(session.state_dir, catalog)['pruned'] == 0
    assert [r['original_text'] for r in session.input_receipts()] == ['before archive', 'during archive']


def test_compaction_admission_barrier_preserves_both_documents(session, monkeypatch):
    from harness.history_compaction_journal import commit_compacted_transcript
    from harness.compaction_archive import load_compaction_archive_page
    original_history = seed_history(session)
    source = load_transcript(session.state_dir, session.harness_session_id)
    target = {**source, 'history': [{'role': 'user', 'content': 'compacted context'}]}
    store = session_input_store(session)
    entered, release, compact_started = threading.Event(), threading.Event(), threading.Event()
    original_write = store._write
    def pause(data):
        entered.set()
        assert release.wait(3)
        original_write(data)
    monkeypatch.setattr(store, '_write', pause)
    def compact():
        compact_started.set()
        commit_compacted_transcript(session.state_dir, session.harness_session_id, source, target, original_history)
    with ThreadPoolExecutor(max_workers=2) as pool:
        admission = pool.submit(session.enqueue_prompt, 'during compaction')
        assert entered.wait(3)
        compaction = pool.submit(compact)
        assert compact_started.wait(3)
        assert not compaction.done()
        release.set()
        assert admission.result(timeout=3)['id']
        compaction.result(timeout=3)
    assert session.input_receipts()[0]['original_text'] == 'during compaction'
    assert load_transcript(session.state_dir, session.harness_session_id) == target
    archived, _ = load_compaction_archive_page(session.state_dir, session.harness_session_id, limit=100)
    assert archived == original_history


def test_missing_receipt_document_never_becomes_empty(session):
    session.enqueue_prompt('irreplaceable original')
    store = session_input_store(session)
    store.path.unlink()
    with pytest.raises(InputReceiptError, match='missing'):
        store.admit('must not overwrite loss')
    assert not store.path.exists()


@pytest.mark.parametrize("mode", ["chat", "auto"])
def test_stream_ack_and_synthetic_provider_see_strict_receipt(session, monkeypatch, mode):
    import io
    from harness.api.streams import StreamServices, stream_chat, stream_auto
    svc_control = services(session)
    image = Path(svc_control.upload_dir) / 'native.png'
    pixels = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl6nAAAAABJRU5ErkJggg==')
    image.write_bytes(pixels)
    document = Path(svc_control.upload_dir) / 'native.txt'
    document.write_text('exact document body')
    original = '  direct original\n'
    calls = []
    class SyntheticPilot:
        name = 'synthetic'
        def chat(self, messages, tools=None, system=None):
            rows = session.input_receipts()
            assert len(rows) == 1 and rows[0]['status'] == 'injected'
            assert rows[0]['original_text'] == original
            disk = load_transcript(session.state_dir, session.harness_session_id)
            assert rows[0]['id'] in disk['history'][-1]['input_ids']
            assert all('input_id' not in row and 'input_ids' not in row for row in messages)
            encoded = json.dumps(messages)
            assert base64.b64encode(pixels).decode() in encoded
            assert 'exact document body' in encoded
            calls.append(True)
            return SimpleNamespace(text='{"say":"synthetic complete","actions":[]}', error=None,
                                   tokens_out=0, tokens_in=0, meta={})
    session.pilot = SyntheticPilot()
    monkeypatch.setattr('harness.vision.pilot_supports_native_images', lambda *_a, **_k: True)
    class Handler:
        wfile = io.BytesIO()
        statuses = []
        def send_response(self, status):
            self.statuses.append(status)
            rows = session.input_receipts()
            assert rows[0]['original_text'] == original
            assert rows[0]['status'] == 'accepted'
        def send_header(self, *_args): pass
        def _cors(self): pass
        def end_headers(self): pass
        def _send(self, status, body):
            raise AssertionError((status, body))
    handler = Handler()
    svc = StreamServices(cfg=session.config, sessions=SimpleNamespace(active='', set_title_if_default=lambda *_: None),
        get_pilot=lambda: session, get_session=lambda: session, ensure_pilot_matches_driver=lambda: None,
        maybe_refresh_codegraph=lambda *_: None, pilot_preflight=lambda: None,
        checkpoint_transcript=lambda *_: None, finalize_turn=lambda *_: None,
        upload_dir=svc_control.upload_dir, auto_budget_from_env=lambda: None)
    if mode == 'chat':
        stream_chat(handler, original, [str(image)], svc, documents=[{'path': str(document)}])
    else:
        stream_auto(handler, original, svc, images=[str(image)], documents=[{'path': str(document)}])
    assert handler.statuses == [200]
    assert calls, handler.wfile.getvalue().decode()
    assert session.input_receipts()[0]['status'] == 'injected'


@pytest.mark.parametrize('native', [False, True])
def test_normalized_steer_images_share_one_input_identity(session, monkeypatch, native):
    svc = services(session)
    image = Path(svc.upload_dir) / 'normalized.png'
    image.write_bytes(b'original bytes')
    monkeypatch.setattr('harness.vision.session_supports_native_images', lambda *_: native)
    monkeypatch.setattr('harness.vision.transcribe_images', lambda *_: [SimpleNamespace(text='transcription', error=None)])
    session._busy.acquire()
    code, result = post_session_steer({'text': ' exact \n', 'images': [str(image)], 'turn_input_mode': 'steer'}, svc)
    assert code == 200, result
    rows = session.input_receipts()
    assert len(rows) == 1 and rows[0]['original_text'] == ' exact \n'
    if native:
        assert len(session._session_actions) == 0
        assert session.list_prompts()[0]['input_id'] == rows[0]['id']
    else:
        assert session.list_prompts() == []
        assert [a.id for a in session._session_actions] == [rows[0]['id']]
        assert 'transcription' in list(session._session_actions)[0].text


def test_document_steer_has_one_original_and_converted_delivery(session):
    svc = services(session)
    document = Path(svc.upload_dir) / 'reference.txt'
    document.write_text('the actual document')
    code, result = post_session_steer({'text': ' original request ', 'documents': [{'path': str(document)}],
                                    'delivery_mode': 'steer'}, svc)
    assert code == 200, result
    rows = session.input_receipts()
    assert len(rows) == 1
    actions = list(session._session_actions)
    assert len(actions) == 1 and actions[0].id == rows[0]['id']
    assert 'the actual document' in actions[0].text
    assert rows[0]['original_text'] == ' original request '


def test_stale_runner_cannot_erase_an_already_published_input(session):
    store = session_input_store(session)
    other = {'history': [{'role': 'user', 'content': 'other backend', 'input_id': 'other-input'}]}
    save_transcript(session.state_dir, session.harness_session_id, other)
    row = store.admit('my new input')
    store.prepare_delivery(row['id'])
    with pytest.raises(InputReceiptError) as failure:
        store.publish_injected([row['id']], {'history': [{'role': 'user', 'content': 'my new input', 'input_id': row['id']}]})
    assert failure.value.code == 'input_publication_conflict'
    assert load_transcript(session.state_dir, session.harness_session_id) == other


def test_stop_drops_playlist_originals_and_preserves_only_new_interrupt_request(session):
    svc = services(session)
    session._busy.acquire()
    first = post_session_queue({'text': 'abandoned queued original'}, svc)[1]
    code, next_request = post_session_steer({'text': 'new interrupt original', 'delivery_mode': 'interrupt'}, svc)
    assert code == 200, next_request
    rows = {r['id']: r for r in session.input_receipts()}
    assert rows[first['input_id']]['status'] == 'dropped'
    assert rows[next_request['input_id']]['status'] == 'accepted'
    assert [item['input_id'] for item in session.list_prompts()] == [next_request['input_id']]
    session.interrupt()
    assert session.list_prompts() == []
    assert all(row['status'] == 'dropped' for row in session.input_receipts())


def test_stop_storage_failure_still_fences_abandoned_queue(session, monkeypatch):
    from harness.api.session_control import post_session_interrupt
    svc = services(session)
    session.enqueue_prompt('retained despite failed Stop write')
    store = session_input_store(session)
    original = store._write
    def fail(_document):
        raise InputReceiptError('input_commit_uncertain', 'injected Stop failure')
    with monkeypatch.context() as patcher:
        patcher.setattr(store, '_write', fail)
        code, result = post_session_interrupt({}, session.harness_session_id, svc)
    assert code == 503 and result['stopped'] is True
    assert session.list_prompts() == []
    assert session.input_receipts()[0]['held'] is True
    assert session.input_receipts()[0]['original_text'] == 'retained despite failed Stop write'


def test_missing_sidecar_cannot_restore_an_outdated_bundle(session):
    from harness import chat_archive
    seed_history(session)
    session.enqueue_prompt('archived original')
    catalog = [{'id': session.harness_session_id, 'archived': True}]
    chat_archive.ingest_all(session.state_dir, sessions=catalog)
    session.enqueue_prompt('newer original not in that backup')
    store = session_input_store(session)
    store.path.unlink()
    with pytest.raises(InputReceiptError):
        store.list()
    assert not store.path.exists()


def test_live_delivering_input_does_not_parse_transcript_on_list(tmp_path, monkeypatch):
    # The queue is polled every few seconds for a whole live turn; only a
    # foreign (cold) owner can be reconciled, so the live owner must not pay
    # a full transcript + archive parse per poll.
    store = InputReceiptStore(str(tmp_path), 'sess-live')
    row = store.admit('hello')
    store.transition(row['id'], 'delivering')
    def must_not_parse():
        raise AssertionError('parsed transcript for a same-instance input')
    monkeypatch.setattr(store, '_durable_ids', must_not_parse)
    assert [r['status'] for r in store.list()] == ['delivering']


def test_unchanged_inputs_file_is_not_revalidated_per_poll(tmp_path, monkeypatch):
    # validate() hashes every row's original text; the queue polls every few
    # seconds, so an unchanged file must not be re-hashed on every read.
    store = InputReceiptStore(str(tmp_path), 'sess-poll')
    for i in range(5):
        store.admit(f'prompt {i} ' + 'x' * 2000)
    calls = []
    real = InputReceiptStore.validate
    monkeypatch.setattr(InputReceiptStore, 'validate', lambda self, data: (calls.append(1), real(self, data))[1])
    for _ in range(4):
        assert len(store.list()) == 5
    assert calls == []
    # A file rewritten outside this process is validated again.
    import os
    path = store.path
    text = path.read_text(encoding='utf-8')
    os.replace(path, str(path) + '.old')
    path.write_text(text, encoding='utf-8')
    assert len(store.list()) == 5
    assert calls
