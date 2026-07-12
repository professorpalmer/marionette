"""Regression tests for the model-swap path. Bug: swapping models (especially
after stop + resend) wiped conversation history and could orphan a live stream.
Verify the backend swap preserves history; mid-turn swaps defer (Hermes-style)
instead of 409 so the composer picker can stage the next prompt's model.
"""
import json
from dataclasses import replace


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
    # Frozen per-runner copy -- mirrors production (_dc_replace / snapshot).
    pilot = ConversationalSession(replace(cfg))
    pilot._history.append({"role": "user", "content": "remember this"})
    monkeypatch.setattr(srv, "_pilot", pilot, raising=False)
    monkeypatch.setattr(srv, "_mcp", None, raising=False)

    h = _FakeHandler()
    srv.Handler._swap_pilot(h, "glm-5.2")
    assert h.sent["code"] == 200
    assert h.sent["body"]["driver"] == "glm-5.2"
    assert h.sent["body"].get("deferred") is False
    assert any(m.get("content") == "remember this" for m in srv._pilot._history)


def test_swap_deferred_while_busy(tmp_path, monkeypatch):
    """Mid-turn picker change stages ``_cfg.driver`` without rebuilding the live pilot."""
    import harness.server as srv
    from harness.conversation import ConversationalSession
    from harness.config import HarnessConfig

    cfg = HarnessConfig(repo=str(tmp_path), state_dir=str(tmp_path / "st2"), driver="glm-5.2")
    monkeypatch.setattr(srv, "_cfg", cfg, raising=False)
    pilot = ConversationalSession(replace(cfg))
    pilot._busy.acquire()  # simulate a turn in progress
    monkeypatch.setattr(srv, "_pilot", pilot, raising=False)
    monkeypatch.setattr(srv, "_mcp", None, raising=False)

    h = _FakeHandler()
    live_before = srv._pilot
    srv.Handler._swap_pilot(h, "deepseek-v4-flash")
    assert h.sent["code"] == 200
    assert h.sent["body"]["deferred"] is True
    assert h.sent["body"]["driver"] == "deepseek-v4-flash"
    # Preference staged; live object unchanged until idle apply.
    assert srv._cfg.driver == "deepseek-v4-flash"
    assert srv._pilot is live_before
    assert srv._pilot.config.driver == "glm-5.2"
    pilot._busy.release()

    assert srv._ensure_pilot_matches_driver() is True
    assert srv._pilot.config.driver == "deepseek-v4-flash"
    assert srv._pilot is not live_before


def test_ensure_skipped_while_busy(tmp_path, monkeypatch):
    import harness.server as srv
    from harness.conversation import ConversationalSession
    from harness.config import HarnessConfig

    cfg = HarnessConfig(repo=str(tmp_path), state_dir=str(tmp_path / "st3"), driver="glm-5.2")
    monkeypatch.setattr(srv, "_cfg", cfg, raising=False)
    pilot = ConversationalSession(replace(cfg))
    pilot._busy.acquire()
    monkeypatch.setattr(srv, "_pilot", pilot, raising=False)
    srv._cfg.driver = "deepseek-v4-flash"
    assert srv._ensure_pilot_matches_driver() is False
    assert srv._pilot.config.driver == "glm-5.2"
    pilot._busy.release()
