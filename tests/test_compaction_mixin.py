"""CompactionContextMixin watermark and fence behavior."""

from harness.compaction_mixin import (
    CompactionContextMixin,
    REASON_WATERMARK_FENCE,
)
from harness.config import HarnessConfig
from harness.conversation import ConversationalSession
from harness.prompt_cache_scope import prompt_cache_scope


class _WatermarkHost(CompactionContextMixin):
    """Minimal host: mixin defines no __init__, so tests supply _history."""

    def __init__(self):
        self._history = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "one"},
            {"role": "assistant", "content": "two"},
            {"role": "user", "content": "three"},
        ]
        self.state_dir = ""
        self.harness_session_id = "sess-physical-must-not-leak"
        self.conversation_key = "lineage-ROOT-1"


def test_get_active_message_watermark_is_max_live_index_or_id():
    host = _WatermarkHost()
    assert host.get_active_message_watermark() == 3
    host._history.append({"role": "user", "content": "four", "id": 12})
    assert host.get_active_message_watermark() == 12


def test_concurrent_tail_remains_after_planned_commit():
    host = _WatermarkHost()
    start_wm = host.get_active_message_watermark()
    recent = [host._history[-1]]
    tail = {"role": "user", "content": "concurrent-tail"}
    host._history.append(tail)
    summary = {
        "role": "user",
        "content": "residual",
        "_compressed_summary": True,
    }
    proposed = host._plan_compacted_history(summary, recent, start_wm)
    assert proposed is not None
    assert proposed[-1] is tail
    host._history[:] = proposed
    assert host._history[-1]["content"] == "concurrent-tail"
    assert host._history[1] is summary


def test_fence_blocks_compact_commit_that_would_drop_the_tail():
    host = _WatermarkHost()
    wm = host.get_active_message_watermark()
    host.mark_commit_watermark_fenced(wm)
    tail = {"role": "user", "content": "concurrent-tail"}
    host._history.append(tail)
    dropping = [
        host._history[0],
        {"role": "user", "content": "residual", "_compressed_summary": True},
    ]
    assert host._admit_compact_commit(dropping) is False
    planned = host._plan_compacted_history(dropping[1], [], wm)
    # Plan clones the live tail, so admission succeeds when the clone is kept.
    assert planned is not None
    assert planned[-1] is tail
    # A rewrite that omits the tail is still refused.
    assert host._admit_compact_commit(dropping) is False
    assert host._history[-1] is tail


def test_history_compaction_fields_include_hashed_scope_not_session_id():
    host = _WatermarkHost()
    fields = host._history_compaction_fields()
    scope = fields.get("prompt_cache_scope")
    assert scope == prompt_cache_scope("lineage-ROOT-1")
    assert host.harness_session_id not in scope
    assert host._prompt_cache_conversation_key() != host.harness_session_id


def test_history_compaction_fields_include_builder_prefix_name():
    from harness.prompt_cache_scope import (
        clear_stable_prefixes,
        prompt_cache_scope,
        register_stable_prefix,
    )

    host = _WatermarkHost()
    host._history = [{"role": "system", "content": "You are Marionette.\n"}]
    register_stable_prefix("system_v1", "You are Marionette.\n")
    try:
        fields = host._history_compaction_fields()
        assert fields.get("prompt_cache_prefix") == "system_v1"
        assert fields.get("prompt_cache_scope") == prompt_cache_scope(
            "lineage-ROOT-1|system_v1"
        )
    finally:
        clear_stable_prefixes()


def _fat_catalog_session(monkeypatch):
    monkeypatch.setattr("harness.compaction_mixin.MIN_COMPACTABLE_TOKENS", 0)
    monkeypatch.delenv("HARNESS_COMPACTION_RESIDUAL", raising=False)
    cfg = HarnessConfig(max_context_tokens=4000)
    session = ConversationalSession(cfg)
    session.harness_session_id = "sess-physical-must-not-leak"
    session.conversation_key = "lineage-ROOT-compact"
    session._history = [{"role": "system", "content": "sys"}]
    for i in range(10):
        session._history.append({
            "role": "user",
            "content": f"User message number {i}: " + ("A" * 150),
        })
        session._history.append({
            "role": "assistant",
            "content": f"Assistant response number {i}: " + ("B" * 150),
        })
    return session


def test_compact_captures_start_watermark_and_keeps_concurrent_tail(monkeypatch):
    session = _fat_catalog_session(monkeypatch)
    before_wm = session.get_active_message_watermark()
    orig = session._injected_residual_content

    def _append_tail_then_inject(summary):
        if session._history[-1].get("content") != "concurrent-tail":
            session._history.append({"role": "user", "content": "concurrent-tail"})
        return orig(summary)

    session._injected_residual_content = _append_tail_then_inject
    events = list(session._maybe_compact_history(force=True))
    assert [e.kind for e in events] == ["compacting", "compaction"]
    assert events[-1].data.get("aborted") is not True
    assert session._compaction_start_watermark == before_wm
    assert session._history[-1]["content"] == "concurrent-tail"


def test_fence_blocks_maybe_compact_when_clone_would_drop_tail(monkeypatch):
    session = _fat_catalog_session(monkeypatch)
    orig = session._injected_residual_content
    original_len = len(session._history)

    def _append_tail_then_omit_clone(summary):
        session.mark_commit_watermark_fenced(session._compaction_start_watermark)
        if session._history[-1].get("content") != "concurrent-tail":
            session._history.append({"role": "user", "content": "concurrent-tail"})
        session._clone_concurrent_tail = lambda _watermark: []
        return orig(summary)

    session._injected_residual_content = _append_tail_then_omit_clone
    events = list(session._maybe_compact_history(force=True))
    assert events[-1].kind == "compaction"
    assert events[-1].data.get("aborted") is True
    assert events[-1].data.get("reason") == REASON_WATERMARK_FENCE
    assert session._history[-1]["content"] == "concurrent-tail"
    assert len(session._history) == original_len + 1
