"""Smoke tests for the PromptQueueMixin extraction.

Guards the mechanical move of prompt-queue persistence and playlist ops out of
harness.conversation into harness.prompt_queue. If the class-hierarchy wiring
or the MRO ever regresses, these fail loudly.
"""

from harness.conversation import ConversationalSession
from harness.prompt_queue import PromptQueueMixin


MOVED_METHODS = (
    "_save_prompt_queue",
    "_load_prompt_queue",
    "enqueue_prompt",
    "list_prompts",
    "remove_prompt",
    "reorder_prompts",
    "clear_prompts",
    "_next_queued_needs_driver_swap",
    "_pop_next_prompt",
)


def test_session_inherits_mixin():
    assert issubclass(ConversationalSession, PromptQueueMixin)
    # And the mixin appears in the MRO.
    assert PromptQueueMixin in ConversationalSession.__mro__


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
        assert attr.__qualname__ == f"PromptQueueMixin.{name}", (
            name,
            attr.__qualname__,
        )


def test_mixin_defines_no_init():
    # The mixin must not carry state or an __init__ of its own -- otherwise
    # it would interfere with ConversationalSession.__init__ via MRO.
    assert "__init__" not in PromptQueueMixin.__dict__


def test_steer_helpers_remain_on_session():
    # _check_and_inject_steer (and its marker helper) stay on ConversationalSession
    # — they are the next peel (SteerMixin), not part of this extraction.
    assert hasattr(ConversationalSession, "_check_and_inject_steer")
    attr = ConversationalSession._check_and_inject_steer
    assert attr.__qualname__ == "ConversationalSession._check_and_inject_steer"
