"""Local PTY evidence: transport acceptance never means agent completion."""
import os
import time
from types import SimpleNamespace

import pytest

from harness.api.terminals import TerminalServices, post_terminal_write
from harness.pty_manager import PtySession


def test_dead_terminal_rejects_submission():
    sess = object.__new__(PtySession)
    sess._alive = False
    svc = TerminalServices(None, SimpleNamespace(get=lambda sid: sess))
    status, body = post_terminal_write({'id': 'old', 'data': 'run\r', 'submission_id': 's1'}, svc)
    assert status == 409
    assert body['submission_id'] == 's1'
    assert body['accepted_bytes'] == 0


def test_partial_write_is_not_success():
    svc = TerminalServices(None, SimpleNamespace(get=lambda sid: SimpleNamespace(write=lambda data: 2)))
    status, body = post_terminal_write({'id': 't', 'data': 'abcd', 'submission_id': 's2'}, svc)
    assert status == 409
    assert body['accepted_bytes'] == 2


@pytest.mark.skipif(os.name == 'nt', reason='Unix controlled PTY fixture')
def test_controlled_delayed_prompt_quiet_and_exit(monkeypatch, tmp_path):
    script = tmp_path / 'fixture.sh'
    script.write_text('#!/bin/sh\nsleep 0.2\nprintf "prompt> "\nsleep 0.3\nread answer\nprintf "received:%s\\n" "$answer"\n')
    script.chmod(0o700)
    monkeypatch.setenv('SHELL', str(script))
    sess = PtySession(cwd=str(tmp_path))
    try:
        assert sess.alive()
        assert sess.read_since(0)[0] == b''
        deadline = time.monotonic() + 4
        while b'prompt> ' not in sess.read_since(0)[0] and time.monotonic() < deadline:
            time.sleep(0.01)
        assert b'prompt> ' in sess.read_since(0)[0]
        assert sess.alive()  # prompt-like bytes while script is still sleeping
        time.sleep(0.4)
        assert sess.alive()  # quiescence and a real read are not completion
        status, receipt = post_terminal_write({'id': sess.id, 'data': 'hello\n', 'submission_id': 'local'}, TerminalServices(None, SimpleNamespace(get=lambda sid: sess)))
        assert status == 200
        assert receipt['accepted_bytes'] == 6
        assert receipt['submission_id'] == 'local'
        while sess.alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not sess.alive()
        assert b'received:hello' in sess.read_since(0)[0]
    finally:
        sess.kill()


@pytest.mark.skipif(os.name == 'nt', reason='Unix controlled PTY fixture')
def test_quiet_pty_stream_reports_unknown_then_exit(monkeypatch, tmp_path):
    import io
    import json
    from harness.api.terminals import stream_terminal

    script = tmp_path / 'quiet.sh'
    script.write_text('#!/bin/sh\nsleep 1.2\nprintf "prompt> "\nsleep 1.2\n')
    script.chmod(0o700)
    monkeypatch.setenv('SHELL', str(script))
    sess = PtySession(cwd=str(tmp_path))
    handler = SimpleNamespace(wfile=io.BytesIO(), send_response=lambda *a: None,
                              send_header=lambda *a: None, _cors=lambda: None,
                              end_headers=lambda: None)
    try:
        stream_terminal(handler, sess.id, TerminalServices(None, SimpleNamespace(get=lambda sid: sess)))
        frames = [json.loads(chunk[6:]) for chunk in handler.wfile.getvalue().decode().split('\n\n') if chunk]
        assert all(frame['id'] == sess.id for frame in frames)
        assert frames[0]['kind'] == 'observation'
        assert frames[0]['state'] == 'unknown'
        assert any(frame['kind'] == 'data' for frame in frames)
        assert frames[-1]['reason'] == 'process_exit'
        assert all(frame.get('state') not in ('ready', 'completed', 'awaiting_input') for frame in frames)
    finally:
        sess.kill()


def test_absolute_cursor_survives_multiple_rollovers():
    import threading
    from harness.pty_manager import _BUFFER_CAP
    sess = object.__new__(PtySession)
    sess._buffer = bytearray()
    sess._total_output = 0
    sess._lock = threading.Lock()
    sess._output_ready = threading.Condition(sess._lock)
    cursor = 0
    for i in range(12):
        chunk = bytes([65 + i]) * (_BUFFER_CAP // 3)
        sess._append_output(chunk)
        data, cursor = sess.read_since(cursor)
        assert data == chunk
        assert cursor == (i + 1) * len(chunk)
        assert sess.read_since(cursor) == (b'', cursor)
        assert len(sess._buffer) <= _BUFFER_CAP
    data, total, start, gap = sess.read_output(0)
    assert len(data) == _BUFFER_CAP
    assert start == total - _BUFFER_CAP
    assert gap == 'trimmed'
    assert sess.read_since(start + 1) == (data[1:], total)
    assert sess.read_output(total + 99)[2:] == (start, 'cursor_ahead')
    for invalid in (-1, None, 'bad', float('inf')):
        assert sess.read_output(invalid)[2:] == (start, 'invalid_cursor')


@pytest.mark.skipif(os.name == 'nt', reason='Unix controlled PTY fixture')
def test_real_pty_multiple_rollovers_reconnect_and_utf8(monkeypatch, tmp_path):
    import codecs
    from harness.pty_manager import _BUFFER_CAP
    script = tmp_path / 'rollover.sh'
    script.write_text('#!/bin/sh\nstty -echo -onlcr\nprintf ready\nwhile read line; do\nhead -c 100000 /dev/zero | tr "\\000" x\nprintf "\\342\\202\\254"\ndone\n')
    script.chmod(0o700)
    monkeypatch.setenv('SHELL', str(script))
    sess = PtySession(cwd=str(tmp_path))
    try:
        deadline = time.monotonic() + 5
        while b'ready' not in sess.read_since(0)[0] and time.monotonic() < deadline:
            time.sleep(.01)
        assert sess.read_since(0)[0] == b'ready'
        cursor = sess.read_since(0)[1]
        for _ in range(9):
            assert sess.write('next\n') == 5
            expected_end = cursor + 100003
            chunks = []
            deadline = time.monotonic() + 5
            while cursor < expected_end and time.monotonic() < deadline:
                data, cursor = sess.read_since(cursor)
                chunks.append(data)
                time.sleep(.005)
            assert b''.join(chunks) == b'x' * 100000 + '€'.encode()
            assert cursor == expected_end
            assert sess.read_since(cursor) == (b'', cursor)
        assert cursor > _BUFFER_CAP * 3
        # Reconnect inside a UTF-8 character: bytes remain exact for xterm's decoder.
        decoder = codecs.getincrementaldecoder('utf-8')()
        assert decoder.decode(sess.read_since(cursor - 3)[0][:1]) == ''
        assert decoder.decode(sess.read_since(cursor - 2)[0]) == '€'
        data, total, start, gap = sess.read_output(0)
        assert gap == 'trimmed' and len(data) == _BUFFER_CAP
        assert total == cursor and start == cursor - _BUFFER_CAP
    finally:
        sess.kill()


@pytest.mark.parametrize('requested,reason', [(0, 'trimmed'), (9999999, 'cursor_ahead')])
def test_stream_gap_reconnect_recovers_exact_retained_bytes(requested, reason):
    import base64
    import io
    import json
    import threading
    from harness.api.terminals import stream_terminal
    from harness.pty_manager import _BUFFER_CAP
    sess = object.__new__(PtySession)
    sess._lock = threading.Lock()
    sess._output_ready = threading.Condition(sess._lock)
    sess._buffer = bytearray()
    sess._total_output = 0
    sess._alive = False
    sess._append_output(b'x' * (_BUFFER_CAP * 3) + b'end')
    def frames(cursor):
        handler = SimpleNamespace(wfile=io.BytesIO(), send_response=lambda *a: None,
                                  send_header=lambda *a: None, _cors=lambda: None,
                                  end_headers=lambda: None)
        stream_terminal(handler, 't', TerminalServices(None, SimpleNamespace(get=lambda sid: sess)), cursor)
        return [json.loads(chunk[6:]) for chunk in handler.wfile.getvalue().decode().split('\n\n') if chunk]
    events = frames(requested)
    assert [e['kind'] for e in events] == ['gap', 'data', 'exit']
    assert events[0]['reason'] == reason
    assert events[0]['offset'] == sess._total_output - _BUFFER_CAP
    assert base64.b64decode(events[1]['b64']) == b'x' * (_BUFFER_CAP - 3) + b'end'
    cursor = events[1]['offset']
    assert [e['kind'] for e in frames(cursor)] == ['exit']
    sess._append_output('€'.encode())
    replay = frames(cursor)
    assert [e['kind'] for e in replay] == ['data', 'exit']
    assert base64.b64decode(replay[0]['b64']) == '€'.encode()


def test_read_snapshot_remains_consistent_during_concurrent_trimming():
    import threading
    from harness.pty_manager import _BUFFER_CAP
    sess = object.__new__(PtySession)
    sess._lock = threading.Lock()
    sess._output_ready = threading.Condition(sess._lock)
    sess._buffer = bytearray()
    sess._total_output = 0
    finished = threading.Event()
    def produce():
        try:
            for _ in range(400):
                sess._append_output(b'x' * 4096)
                time.sleep(.0001)
        finally:
            finished.set()
    producer = threading.Thread(target=produce)
    producer.start()
    try:
        cursor = 0
        while not finished.is_set():
            data, total, start, reason = sess.read_output(cursor)
            assert total >= cursor
            assert len(data) == total - start <= _BUFFER_CAP
            assert data == b'x' * len(data)
            assert start == cursor or reason == 'trimmed'
            cursor = total
            time.sleep(.0002)
    finally:
        producer.join()
    assert sess.read_output(0)[1] == 400 * 4096


@pytest.mark.skipif(os.name == 'nt', reason='Unix controlled PTY fixture')
def test_idle_stream_waits_for_output_instead_of_polling(monkeypatch, tmp_path):
    import io
    import json
    from harness.api.terminals import stream_terminal

    script = tmp_path / 'idle.sh'
    script.write_text('#!/bin/sh\nprintf "ready"\nsleep 2.2\nprintf "late"\n')
    script.chmod(0o700)
    monkeypatch.setenv('SHELL', str(script))
    sess = PtySession(cwd=str(tmp_path))
    reads = []
    real = sess.read_output
    sess.read_output = lambda offset: reads.append(offset) or real(offset)
    handler = SimpleNamespace(wfile=io.BytesIO(), send_response=lambda *a: None,
                              send_header=lambda *a: None, _cors=lambda: None,
                              end_headers=lambda: None)
    try:
        stream_terminal(handler, sess.id, TerminalServices(None, SimpleNamespace(get=lambda sid: sess)))
        frames = [json.loads(c[6:]) for c in handler.wfile.getvalue().decode().split('\n\n') if c]
        text = b''.join(__import__('base64').b64decode(f['b64']) for f in frames if f['kind'] == 'data')
        assert b'ready' in text and b'late' in text
        # ~2.2 s idle: a 20 Hz poll made ~45 reads; waiting wakes on output and the 1 Hz observation.
        assert len(reads) <= 12, len(reads)
    finally:
        sess.kill()
