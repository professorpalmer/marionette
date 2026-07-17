"""Smoke tests for the CompactionContextMixin extraction.

Guards the mechanical move of compaction / token / elision helpers out of
harness.conversation into harness.compaction_mixin. If the class-hierarchy
wiring or the MRO ever regresses, these fail loudly.
"""

from harness.compaction_mixin import CompactionContextMixin
from harness.conversation import ConversationalSession


MOVED_METHODS = (
    "_estimate_context_tokens_for_list",
    "_invalidate_ctx_cache",
    "_estimate_context_tokens",
    "_find_safe_split",
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
