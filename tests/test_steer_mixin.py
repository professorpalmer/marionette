"""Smoke tests for the SteerMixin extraction.

Guards the mechanical move of mid-turn steer enqueue/drain/inject helpers out of
harness.conversation into harness.steer_mixin. If the class-hierarchy wiring
or the MRO ever regresses, these fail loudly.
"""

from harness.conversation import ConversationalSession
from harness.steer_mixin import SteerMixin


MOVED_METHODS = (
    "steer_with_images",
    "enqueue_steer",
    "drain_steer",
    "drop_queued_steers",
    "_steer_boundary_blocks_inject",
    "_record_steer_drop_notice",
    "_flush_steer_drop_notice",
    "_steer_marker",
    "_check_and_inject_steer",
    "_tool_result_is_adjacent",
)


def test_session_inherits_mixin():
    assert issubclass(ConversationalSession, SteerMixin)
    # And the mixin appears in the MRO.
    assert SteerMixin in ConversationalSession.__mro__


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
        assert attr.__qualname__ == f"SteerMixin.{name}", (
            name,
            attr.__qualname__,
        )


def test_mixin_defines_no_init():
    # The mixin must not carry state or an __init__ of its own -- otherwise
    # it would interfere with ConversationalSession.__init__ via MRO.
    assert "__init__" not in SteerMixin.__dict__


def test_prompt_queue_crud_stays_on_prompt_queue_mixin():
    # Prompt-queue playlist CRUD was a prior peel and must not have been
    # swept into SteerMixin.
    from harness.prompt_queue import PromptQueueMixin
    for name in ("enqueue_prompt", "list_prompts", "clear_prompts", "_pop_next_prompt"):
        attr = getattr(ConversationalSession, name)
        assert attr.__qualname__ == f"PromptQueueMixin.{name}", (
            name,
            attr.__qualname__,
        )
