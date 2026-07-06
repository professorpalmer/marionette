"""Tests for read-only L0-L3 memory layer snapshot helpers."""
import json
import os

from harness.memory_layers import (
    LAYER_IDS,
    estimate_l0_hot_chars,
    measure_l1_session,
    measure_l2_workspace,
    measure_l3_cold,
    snapshot_memory_layers,
)
from harness.spill_registry import register_spill


class _FakeConversation:
    def __init__(self, history):
        self._history = history


def test_l0_increases_when_history_grows():
    small = _FakeConversation(
        [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hi"},
        ]
    )
    large = _FakeConversation(
        [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello there"},
            {"role": "user", "content": "x" * 500},
        ]
    )
    assert estimate_l0_hot_chars(large) > estimate_l0_hot_chars(small)


def test_l1_reflects_registered_spill(tmp_path):
    state = str(tmp_path)
    path = os.path.join(state, "pmharness-results", "call1.txt")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("spilled content")
    register_spill(state, "sess1", "call1", path, len("spilled content"))

    layer = measure_l1_session(state, "sess1")
    assert layer["bytes"] > 0
    assert layer["entries"] >= 1
    assert layer["components"]["spill_entries"] >= 1


def test_l2_missing_stores_return_zeros(tmp_path):
    layer = measure_l2_workspace(str(tmp_path), repo=str(tmp_path))
    assert layer == {"bytes": 0, "entries": 0, "components": {}}


def test_l3_missing_stores_return_zeros(tmp_path):
    layer = measure_l3_cold(str(tmp_path), "sess1")
    assert layer["bytes"] == 0
    assert layer["entries"] == 0
    assert "components" in layer


def test_snapshot_shape(tmp_path):
    conv = _FakeConversation([{"role": "system", "content": "s"}, {"role": "user", "content": "q"}])
    snap = snapshot_memory_layers(conv, str(tmp_path), "default", repo=str(tmp_path))
    assert set(snap.keys()) == set(LAYER_IDS) | {"snapshot_at"}
    for layer_id in LAYER_IDS:
        assert "bytes" in snap[layer_id]
        assert "entries" in snap[layer_id]
    assert snap["L0"]["bytes"] > 0
    assert isinstance(snap["snapshot_at"], str)


def test_l1_turn_context_lines_counted(tmp_path):
    state = str(tmp_path)
    from harness.turn_context import record_turn_context

    record_turn_context(state, "s1", 1, repo=str(tmp_path))
    record_turn_context(state, "s1", 2, repo=str(tmp_path))
    layer = measure_l1_session(state, "s1")
    assert layer["components"]["turn_context_lines"] == 2
    assert layer["bytes"] > 0


def test_measurement_never_raises_on_bad_paths():
    conv = _FakeConversation([])
    snap = snapshot_memory_layers(conv, "", "default", repo="")
    for layer_id in LAYER_IDS:
        assert snap[layer_id]["bytes"] == 0
        assert snap[layer_id]["entries"] == 0
