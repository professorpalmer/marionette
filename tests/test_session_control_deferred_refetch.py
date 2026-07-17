"""Characterization: session_control re-fetches pilot after deferred gate swap.

Product handlers call ``gate_active_pilot_ready`` then ``get_pilot()`` again so
mutations never land on a ``DeferredPilotPlaceholder`` that ``ensure_ready``
just replaced. HTTP integration covers the wait path; these unit fakes assert
identity re-fetch itself (pre-gate placeholder vs post-gate real pilot).
"""
from __future__ import annotations

from types import SimpleNamespace

from harness.api.session_control import (
    SessionControlServices,
    post_session_queue,
    post_session_rewind,
    post_session_steer,
)
from harness.deferred_attach import DeferredPilotPlaceholder


class _RealPilot:
    """Mutation target that records which methods ran after a gate swap."""

    def __init__(self) -> None:
        self.rewinds: list[int] = []
        self.steers: list[str] = []
        self.prompts: list[dict] = []

    def rewind_to_user_ordinal(self, n: int) -> dict:
        self.rewinds.append(n)
        return {"ok": True, "user_ordinal": n}

    def enqueue_steer(self, text: str) -> None:
        self.steers.append(text)

    def enqueue_prompt(self, text: str, images=None, model=None) -> dict:
        item = {"id": "q1", "text": text, "model": model}
        self.prompts.append(item)
        return item


def _svc_with_gate_swap(placeholder, real, *, upload_dir: str = "/uploads"):
    """Active pilot starts as placeholder; gate swaps it to ``real``."""
    box: dict = {"pilot": placeholder, "gate_calls": 0, "get_snapshots": []}

    def get_pilot():
        current = box["pilot"]
        box["get_snapshots"].append(current)
        return current

    def gate_active_pilot_ready():
        # Mirrors ensure_active_pilot_ready: swap registry identity, then ready.
        box["gate_calls"] += 1
        box["pilot"] = real
        return None

    svc = SessionControlServices(
        cfg=SimpleNamespace(driver="m1", state_dir=None, max_context_tokens=96000),
        get_pilot=get_pilot,
        get_runners=lambda: SimpleNamespace(get=lambda sid: None),
        gate_active_pilot_ready=gate_active_pilot_ready,
        stash_put=lambda msg, imgs: "mid1",
        save_active_transcript=lambda: None,
        upload_dir=upload_dir,
        diag=lambda *a: None,
    )
    return svc, box


def _placeholder() -> DeferredPilotPlaceholder:
    return DeferredPilotPlaceholder(
        session_id="s-deferred",
        state_dir="/tmp",
        transcript=[],
    )


def test_rewind_refetches_pilot_after_deferred_gate_swap():
    """Rewind must call methods on the post-gate pilot, not the placeholder."""
    placeholder = _placeholder()
    assert not hasattr(placeholder, "rewind_to_user_ordinal")
    real = _RealPilot()
    svc, box = _svc_with_gate_swap(placeholder, real)

    code, payload = post_session_rewind({"user_ordinal": 1}, svc)

    assert code == 200 and payload["ok"] is True
    assert real.rewinds == [1]
    assert box["gate_calls"] == 1
    assert any(p is placeholder for p in box["get_snapshots"])
    assert box["get_snapshots"][-1] is real


def test_steer_refetches_pilot_after_deferred_gate_swap():
    """Steer must enqueue on the post-gate pilot after ensure_ready swap."""
    placeholder = _placeholder()
    assert not hasattr(placeholder, "enqueue_steer")
    real = _RealPilot()
    svc, box = _svc_with_gate_swap(placeholder, real)

    code, payload = post_session_steer({"text": "nudge"}, svc)

    assert code == 200 and payload["ok"] is True
    assert real.steers == ["nudge"]
    assert box["gate_calls"] == 1
    assert any(p is placeholder for p in box["get_snapshots"])
    assert box["get_snapshots"][-1] is real


def test_queue_refetches_pilot_after_deferred_gate_swap():
    """Queue enqueue must target the swapped real pilot, not the placeholder."""
    placeholder = _placeholder()
    assert not hasattr(placeholder, "enqueue_prompt")
    real = _RealPilot()
    svc, box = _svc_with_gate_swap(placeholder, real)

    code, payload = post_session_queue({"text": "later"}, svc)

    assert code == 200 and payload["ok"] is True
    assert payload["item"]["id"] == "q1"
    assert real.prompts == [{"id": "q1", "text": "later", "model": "m1"}]
    assert box["gate_calls"] == 1
    assert any(p is placeholder for p in box["get_snapshots"])
    assert box["get_snapshots"][-1] is real
