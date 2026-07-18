"""Smoke tests for the BusyControlMixin extraction.

Guards the mechanical move of busy-lifecycle helpers out of
harness.conversation into harness.busy_control. If the class-hierarchy
wiring or the MRO ever regresses, these fail loudly.
"""

from harness.busy_control import BusyControlMixin
from harness.conversation import ConversationalSession


MOVED_METHODS = (
    "is_turn_busy",
    "interrupt",
    "_drain_session_jobs_dual_store",
    "_mark_busy_acquired",
    "_release_busy",
    "_turn_deadline_seconds",
    "_reap_stuck_turn",
)


def test_session_inherits_mixin():
    assert issubclass(ConversationalSession, BusyControlMixin)
    assert BusyControlMixin in ConversationalSession.__mro__


def test_moved_methods_present_on_session():
    for name in MOVED_METHODS:
        assert hasattr(ConversationalSession, name), name
        attr = getattr(ConversationalSession, name)
        assert callable(attr), name


def test_moved_methods_resolve_to_mixin():
    for name in MOVED_METHODS:
        attr = getattr(ConversationalSession, name)
        assert attr.__qualname__ == f"BusyControlMixin.{name}", (
            name,
            attr.__qualname__,
        )


def test_mixin_defines_no_init():
    assert "__init__" not in BusyControlMixin.__dict__


def test_send_loop_not_folded_into_busy_control():
    # Full turn loop body lives on SendLoopMixin (separate peel).
    from harness.send_loop import SendLoopMixin

    for name in ("send", "_send_locked", "_send_locked_inner"):
        assert hasattr(ConversationalSession, name), name
        attr = getattr(ConversationalSession, name)
        assert attr.__qualname__ == f"SendLoopMixin.{name}", (
            name,
            attr.__qualname__,
        )
        assert not attr.__qualname__.startswith("BusyControlMixin."), name


def test_cancel_stays_on_session():
    # cancel() is the cooperative signal; interrupt() (mixin) calls it.
    attr = getattr(ConversationalSession, "cancel")
    assert attr.__qualname__ == "ConversationalSession.cancel", attr.__qualname__
