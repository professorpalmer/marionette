"""Live-path validation for v0.9.216 phantom-steer / session-scoped resume.

Pins the operator regression: empty busy Stop must not invent a queued steer
that interrupt then drops as ``steer_dropped``. Also covers empty steer 400,
session-scoped resume latch peek over HTTP, and task_profile on send.

HTTP coverage reuses the lifecycle helpers from ``test_self_dev_restart`` so
we do not duplicate the ThreadingHTTPServer fixture.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from harness.config import HarnessConfig
from harness.conversation import ConversationalSession
from test_self_dev_restart import _harness_http_server, _session_state

_HTTP_TIMEOUT = 10.0


def _auth_headers(token: str) -> dict:
    return {
        "Content-Type": "application/json",
        "X-Harness-Token": token,
    }


def _post_json(port: int, path: str, body: dict, token: str):
    req = urllib.request.Request(
        "http://127.0.0.1:%d%s" % (port, path),
        data=json.dumps(body).encode("utf-8"),
        headers=_auth_headers(token),
        method="POST",
    )
    return urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT)


def _clear_stop_markers(pilot) -> None:
    """Leave the shared module pilot ready for later suite tests."""
    pilot._stop_holds_idle = False
    pilot._steer_boundary_drop_on_acquire = False
    pilot._interrupt_requested = False
    pilot._pending_steer_drop_notice = None
    pilot._cancel.clear()
    try:
        if pilot._busy.locked():
            pilot._busy.release()
    except RuntimeError:
        pass
    pilot._state = "idle"
    pilot.drain_steer()


def _notices_have_steer_drop(notices) -> bool:
    for notice in notices or []:
        if not isinstance(notice, dict):
            continue
        if notice.get("reason") == "steer_dropped":
            return True
        message = str(notice.get("message") or "")
        if "Dropped" in message and "queued steer" in message:
            return True
    return False


def _wait_busy(pilot, timeout: float = 20.0, errors=None) -> None:
    """Wait until send() holds ``_busy``.

    ``GET /api/chat`` does driver-match, CodeGraph refresh, and mention
    expansion before ``send()`` acquires the lock. Five seconds flakes on
    CI when those pre-lock steps contend with blocked catalog fetches.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if errors:
            raise AssertionError("chat thread failed before busy: %r" % (errors,))
        try:
            if pilot._busy.locked():
                return
        except Exception:
            pass
        time.sleep(0.02)
    raise AssertionError("expected an open busy turn")


def test_http_interrupt_empty_busy_turn_has_no_phantom_steer_drop():
    """Open turn + Stop with no steer text must not paint steer_dropped."""
    release = threading.Event()

    with _harness_http_server() as (srv, port):
        pilot = srv._pilot
        _clear_stop_markers(pilot)
        original_driver = pilot.pilot

        class _SlowPilot:
            name = "stub-oracle-v2"

            def complete(self, prompt, *, system=None):
                from pmharness.drivers.openai_compat import DriverResponse

                release.wait(timeout=8.0)
                return DriverResponse(
                    text='{"say":"done","actions":[]}',
                    tokens_out=10,
                    latency_ms=1.0,
                )

        # Chat start rebuilds when config.driver lags _cfg.driver. Keep them
        # aligned so this stub stays on the object that handles the request.
        try:
            want = str(getattr(srv._cfg, "driver", "") or "").strip()
            if want and getattr(pilot, "config", None) is not None:
                pilot.config.driver = want
        except Exception:
            pass
        pilot.pilot = _SlowPilot()
        errors = []

        def _run_chat():
            try:
                msg = urllib.parse.quote("hello")
                req = urllib.request.Request(
                    "http://127.0.0.1:%d/api/chat?message=%s" % (port, msg),
                    headers={"X-Harness-Token": srv._TOKEN},
                    method="GET",
                )
                with urllib.request.urlopen(req, timeout=20.0) as resp:
                    resp.read()
            except Exception as exc:
                errors.append(exc)

        chat_thread = threading.Thread(target=_run_chat, name="v09216-chat", daemon=True)
        chat_thread.start()
        try:
            # Watch the live module pilot — a prior test that left
            # config.driver mismatched can rebuild _pilot on chat start.
            _wait_busy(srv._pilot, errors=errors)
            assert list(pilot._steer_queue) == []

            with _post_json(port, "/api/session/interrupt", {}, srv._TOKEN) as resp:
                assert resp.status == 200
                payload = json.loads(resp.read().decode("utf-8"))
            assert payload.get("ok") is True
            assert not _notices_have_steer_drop(payload.get("notices"))
            assert list(pilot._steer_queue) == []
            assert getattr(pilot, "_pending_steer_drop_notice", None) in (None, {})
        finally:
            release.set()
            chat_thread.join(timeout=5.0)
            pilot.pilot = original_driver
            _clear_stop_markers(pilot)
        assert not errors, "chat thread failed: %r" % (errors,)


def test_http_empty_steer_rejected_interrupt_still_clean():
    """Empty steer is 400; queue stays empty; Stop still has no drop notice."""
    with _harness_http_server() as (srv, port):
        pilot = srv._pilot
        _clear_stop_markers(pilot)
        try:
            # Hold busy so this mirrors a mid-turn empty composer Stop path.
            assert pilot._busy.acquire(blocking=False)
            pilot._state = "executing"
            assert list(pilot._steer_queue) == []

            try:
                _post_json(port, "/api/session/steer", {"text": ""}, srv._TOKEN)
                assert False, "empty steer must return 400"
            except urllib.error.HTTPError as err:
                assert err.code == 400
                body = json.loads(err.read().decode("utf-8"))
                assert "missing text" in str(body.get("error") or "")

            try:
                _post_json(
                    port,
                    "/api/session/steer",
                    {"text": "   ", "images": []},
                    srv._TOKEN,
                )
                assert False, "whitespace-only steer must return 400"
            except urllib.error.HTTPError as err:
                assert err.code == 400

            assert list(pilot._steer_queue) == []
            assert pilot.drain_steer() == []

            with _post_json(port, "/api/session/interrupt", {}, srv._TOKEN) as resp:
                assert resp.status == 200
                payload = json.loads(resp.read().decode("utf-8"))
            assert payload.get("ok") is True
            assert not _notices_have_steer_drop(payload.get("notices"))
            assert getattr(pilot, "_pending_steer_drop_notice", None) in (None, {})
        finally:
            _clear_stop_markers(pilot)


def test_http_resume_latch_armed_for_a_not_peek_true_for_b():
    """Latch armed for session A must not peek true for session B over HTTP."""
    with _harness_http_server() as (srv, port):
        saved_history = list(srv._pilot._history)
        saved_active = srv._sessions._active
        try:
            srv._clear_resume_latch()
            sid_a = "sess-resume-a"
            sid_b = "sess-resume-b"
            srv._sessions._active = sid_a
            srv._pilot._history = [
                {"role": "system", "content": "sys"},
                {"role": "user", "content": "please continue"},
            ]
            srv._set_resume_latch(sid_a)

            data_a = _session_state(port, srv._TOKEN, session_id=sid_a)
            assert data_a["resume_pending"] is True
            data_b = _session_state(port, srv._TOKEN, session_id=sid_b)
            assert data_b["resume_pending"] is False
            # Wrong-session consume must not steal A's latch.
            assert (
                _session_state(
                    port,
                    srv._TOKEN,
                    consume_resume=True,
                    session_id=sid_b,
                )["resume_pending"]
                is False
            )
            assert srv._resume_latch is True
            assert (
                _session_state(port, srv._TOKEN, session_id=sid_a)["resume_pending"]
                is True
            )
        finally:
            srv._pilot._history = saved_history
            srv._sessions._active = saved_active
            srv._clear_resume_latch()


def test_conversational_send_emits_task_profile(tmp_path):
    """Cheap live-path pin: send still emits ConvEvent task_profile."""
    cfg = HarnessConfig(
        driver="stub-oracle-v2",
        state_dir=str(tmp_path / "state"),
        repo=str(tmp_path / "repo"),
    )
    (tmp_path / "repo").mkdir(exist_ok=True)
    (tmp_path / "state").mkdir(exist_ok=True)
    session = ConversationalSession(cfg)
    session.harness_session_id = "live-216"
    events = list(session.send("typo in README.md"))
    assert "task_profile" in [e.kind for e in events]
