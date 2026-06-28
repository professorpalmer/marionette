"""Regression tests for the model-swap path. Bug: swapping models (especially
after stop + resend) wiped conversation history and could orphan a live stream.
Verify the backend swap preserves history and refuses mid-stream.
"""
import json


class _FakeHandler:
    def __init__(self):
        self.sent = {}
    def _send(self, code, body):
        self.sent = {"code": code, "body": json.loads(body)}


def test_swap_preserves_history(tmp_path, monkeypatch):
    import harness.server as srv
    from harness.conversation import ConversationalSession
    from harness.config import HarnessConfig

    cfg = HarnessConfig(repo=str(tmp_path), state_dir=str(tmp_path / "st"))
    monkeypatch.setattr(srv, "_cfg", cfg, raising=False)
    pilot = ConversationalSession(cfg)
    pilot._history.append({"role": "user", "content": "remember this"})
    monkeypatch.setattr(srv, "_pilot", pilot, raising=False)
    monkeypatch.setattr(srv, "_mcp", None, raising=False)

    h = _FakeHandler()
    srv.Handler._swap_pilot(h, "glm-5.2")
    assert h.sent["code"] == 200
    assert h.sent["body"]["driver"] == "glm-5.2"
    assert any(m.get("content") == "remember this" for m in srv._pilot._history)


def test_swap_refused_while_busy(tmp_path, monkeypatch):
    import harness.server as srv
    from harness.conversation import ConversationalSession
    from harness.config import HarnessConfig

    cfg = HarnessConfig(repo=str(tmp_path), state_dir=str(tmp_path / "st2"))
    monkeypatch.setattr(srv, "_cfg", cfg, raising=False)
    pilot = ConversationalSession(cfg)
    pilot._busy.acquire()  # simulate a turn in progress
    monkeypatch.setattr(srv, "_pilot", pilot, raising=False)
    monkeypatch.setattr(srv, "_mcp", None, raising=False)

    h = _FakeHandler()
    srv.Handler._swap_pilot(h, "glm-5.2")
    assert h.sent["code"] == 409
    pilot._busy.release()
