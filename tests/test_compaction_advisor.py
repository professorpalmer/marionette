"""Tests for layer-pressure compaction advisor."""

from harness.compaction_advisor import (
    _HOT_L1_COMBO_RATIO,
    _HOT_NOW_RATIO,
    _HOT_SOON_RATIO,
    _L1_PRESSURE_BYTES,
    advice_payload,
    assess_layer_pressure,
)
from harness.memory_layers import record_memory_layer_snapshot, snapshot_memory_layers


class _FakeConversation:
    def __init__(self, history):
        self._history = history


def _snapshot(l0_bytes: int, l1_bytes: int = 0, l3_before: int = 0, l3_after: int = 0) -> dict:
    l3_components = {}
    if l3_before or l3_after:
        l3_components = {
            "compaction_chars_before": l3_before,
            "compaction_chars_after": l3_after,
        }
    return {
        "L0": {"bytes": l0_bytes, "entries": 1},
        "L1": {"bytes": l1_bytes, "entries": 0, "components": {}},
        "L2": {"bytes": 0, "entries": 0, "components": {}},
        "L3": {"bytes": max(0, l3_before - l3_after), "entries": 0, "components": l3_components},
        "snapshot_at": "2026-07-06T12:00:00+00:00",
    }


def test_hot_now_boundary_below():
    budget = 1000
    l0 = int(budget * 4 * (_HOT_NOW_RATIO - 0.01))
    advice = assess_layer_pressure(_snapshot(l0), budget)
    assert advice["level"] != "now"


def test_hot_now_boundary_at():
    budget = 1000
    l0 = int(budget * 4 * _HOT_NOW_RATIO)
    advice = assess_layer_pressure(_snapshot(l0), budget)
    assert advice["level"] == "now"
    assert advice["reasons"]


def test_hot_soon_boundary_below():
    budget = 1000
    l0 = int(budget * 4 * (_HOT_SOON_RATIO - 0.01))
    advice = assess_layer_pressure(_snapshot(l0), budget)
    assert advice["level"] == "none"


def test_hot_soon_boundary_at():
    budget = 1000
    l0 = int(budget * 4 * _HOT_SOON_RATIO)
    advice = assess_layer_pressure(_snapshot(l0), budget)
    assert advice["level"] == "soon"
    assert "hot context" in advice["reasons"][0]


def test_l1_pressure_boundary_below_bytes():
    budget = 1000
    l0 = int(budget * 4 * _HOT_L1_COMBO_RATIO)
    advice = assess_layer_pressure(_snapshot(l0, l1_bytes=_L1_PRESSURE_BYTES), budget)
    assert advice["level"] == "none"


def test_l1_pressure_promotes_none_to_soon():
    budget = 1000
    l0 = int(budget * 4 * _HOT_L1_COMBO_RATIO)
    advice = assess_layer_pressure(_snapshot(l0, l1_bytes=_L1_PRESSURE_BYTES + 1), budget)
    assert advice["level"] == "soon"
    assert advice["l1_bytes"] > _L1_PRESSURE_BYTES
    assert "session state exceeds 5 MB" in advice["reasons"][0]


def test_malformed_snapshot_returns_none():
    assert assess_layer_pressure({}, 1000)["level"] == "none"
    assert assess_layer_pressure({"L0": "bad"}, 1000)["level"] == "none"
    assert assess_layer_pressure(_snapshot(100), 0)["level"] == "none"


def test_hot_ratio_clamped():
    budget = 1000
    huge = budget * 4 * 3
    advice = assess_layer_pressure(_snapshot(huge), budget)
    assert advice["hot_ratio"] == 2.0
    assert advice["level"] == "now"


def test_l3_reclaimed_bytes_from_components():
    advice = assess_layer_pressure(_snapshot(0, l3_before=5000, l3_after=2000), 1000)
    assert advice["l3_reclaimed_bytes"] == 3000


def test_advice_payload_disabled(monkeypatch, tmp_path):
    monkeypatch.setenv("HARNESS_COMPACTION_ADVISOR", "off")
    state = str(tmp_path)
    snap = snapshot_memory_layers(_FakeConversation([]), state, "s1")
    record_memory_layer_snapshot(state, "s1", 1, snap)
    assert advice_payload(state, "s1", 96000) == {}


def test_advice_payload_round_trip_from_journal(tmp_path):
    state = str(tmp_path)
    conv = _FakeConversation([{"role": "system", "content": "s"}, {"role": "user", "content": "hello"}])
    snap = snapshot_memory_layers(conv, state, "s1")
    record_memory_layer_snapshot(state, "s1", 1, snap)
    payload = advice_payload(state, "s1", 96000)
    assert "compaction_advice" in payload
    advice = payload["compaction_advice"]
    assert advice["level"] in ("none", "soon", "now")
    assert isinstance(advice["hot_ratio"], float)
    assert isinstance(advice["reasons"], list)


def test_advice_payload_empty_when_no_journal(tmp_path):
    assert advice_payload(str(tmp_path), "missing", 96000) == {}


def test_assess_never_raises_on_bad_input():
    assess_layer_pressure(None, 1000)  # type: ignore[arg-type]
    assess_layer_pressure({"L0": {"bytes": "x"}}, 1000)
