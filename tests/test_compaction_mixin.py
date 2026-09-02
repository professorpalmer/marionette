"""Smoke tests for the CompactionContextMixin extraction.

Guards the mechanical move of compaction / token / elision helpers out of
harness.conversation into harness.compaction_mixin. If the class-hierarchy
wiring or the MRO ever regresses, these fail loudly.
"""

from harness.compaction_mixin import (
    CompactionContextMixin,
    REASON_WATERMARK_FENCE,
)
from harness.config import HarnessConfig
from harness.conversation import ConversationalSession
from harness.prompt_cache_scope import prompt_cache_scope


MOVED_METHODS = (
    "_estimate_context_tokens_for_list",
    "_invalidate_ctx_cache",
    "_estimate_context_tokens",
    "_find_safe_split",
    "_choose_compaction_split",
    "_minimum_recent_start",
    "_history_compaction_fields",
    "_format_block_for_summary",
    "_make_fallback_summary",
    "_maybe_compact_history",
    "_elide_stale_reads",
    "_extract_read_text",
)


def test_session_inherits_mixin():
    assert issubclass(ConversationalSession, CompactionContextMixin)
    # And the mixin appears in the MRO.
    assert CompactionContextMixin in ConversationalSession.__mro__


def test_moved_methods_present_on_session():
    for name in MOVED_METHODS:
        assert hasattr(ConversationalSession, name), name
        attr = getattr(ConversationalSession, name)
        assert callable(attr), name


def test_moved_methods_resolve_to_mixin():
    # __qualname__ tells us where the method is actually defined; if any of
    # these regress to "ConversationalSession.*" it means the extraction was
    # accidentally partially reverted or shadowed.
    for name in MOVED_METHODS:
        attr = getattr(ConversationalSession, name)
        assert attr.__qualname__ == f"CompactionContextMixin.{name}", (
            name,
            attr.__qualname__,
        )


def test_mixin_defines_no_init():
    # The mixin must not carry state or an __init__ of its own -- otherwise
    # it would interfere with ConversationalSession.__init__ via MRO.
    assert "__init__" not in CompactionContextMixin.__dict__


def test_send_loop_not_folded_into_compaction():
    # The send loop lives on SendLoopMixin (separate peel).
    from harness.send_loop import SendLoopMixin

    for name in ("send", "_send_locked_inner"):
        attr = getattr(ConversationalSession, name)
        assert attr.__qualname__ == f"SendLoopMixin.{name}", (
            name,
            attr.__qualname__,
        )


def test_steer_and_prompt_queue_not_folded_in():
    from harness.prompt_queue import PromptQueueMixin
    from harness.steer_mixin import SteerMixin

    for name in ("enqueue_prompt", "list_prompts", "clear_prompts", "_pop_next_prompt"):
        attr = getattr(ConversationalSession, name)
        assert attr.__qualname__ == f"PromptQueueMixin.{name}", (
            name,
            attr.__qualname__,
        )
    for name in ("enqueue_steer", "drain_steer", "_check_and_inject_steer"):
        attr = getattr(ConversationalSession, name)
        assert attr.__qualname__ == f"SteerMixin.{name}", (
            name,
            attr.__qualname__,
        )


NEW_WATERMARK_METHODS = (
    "get_active_message_watermark",
    "mark_commit_watermark_fenced",
    "_clone_concurrent_tail",
    "_admit_compact_commit",
    "_plan_compacted_history",
)


def test_watermark_methods_resolve_to_mixin():
    for name in NEW_WATERMARK_METHODS:
        assert hasattr(ConversationalSession, name), name
        attr = getattr(ConversationalSession, name)
        assert attr.__qualname__ == f"CompactionContextMixin.{name}", (
            name,
            attr.__qualname__,
        )


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
