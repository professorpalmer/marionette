"""Smoke tests for the ReviewMemoryMixin extraction.

Guards the mechanical move of apply/dismiss review and memory-proposal helpers
out of harness.conversation into harness.review_memory. If the class-hierarchy
wiring or the MRO ever regresses, these fail loudly.
"""

from harness.conversation import ConversationalSession
from harness.review_memory import ReviewMemoryMixin


MOVED_METHODS = (
    "apply_review",
    "dismiss_review",
    "_flush_turn_memory_proposals",
    "accept_memory_proposal",
    "dismiss_memory_proposal",
)


def test_session_inherits_mixin():
    assert issubclass(ConversationalSession, ReviewMemoryMixin)
    assert ReviewMemoryMixin in ConversationalSession.__mro__


def test_moved_methods_present_on_session():
    for name in MOVED_METHODS:
        assert hasattr(ConversationalSession, name), name
        attr = getattr(ConversationalSession, name)
        assert callable(attr), name


def test_moved_methods_resolve_to_mixin():
    for name in MOVED_METHODS:
        attr = getattr(ConversationalSession, name)
        assert attr.__qualname__ == f"ReviewMemoryMixin.{name}", (
            name,
            attr.__qualname__,
        )


def test_mixin_defines_no_init():
    assert "__init__" not in ReviewMemoryMixin.__dict__


def test_apply_worker_patch_stays_on_session():
    # Host-coupled patch apply must not have been swept into the mixin.
    attr = getattr(ConversationalSession, "_apply_worker_patch")
    assert attr.__qualname__ == "ConversationalSession._apply_worker_patch", (
        attr.__qualname__,
    )


def test_busy_send_swarm_remain_on_session():
    for name in ("_send_locked", "_send_locked_inner", "_await_and_apply_job"):
        assert hasattr(ConversationalSession, name), name
        attr = getattr(ConversationalSession, name)
        assert attr.__qualname__ == f"ConversationalSession.{name}", (
            name,
            attr.__qualname__,
        )
