"""Smoke tests for the SendLoopMixin extraction.

Guards the mechanical move of send-loop orchestration out of
harness.conversation into harness.send_loop. If the class-hierarchy
wiring or the MRO ever regresses, these fail loudly.
"""

from harness.conversation import ConversationalSession
from harness.send_loop import SendLoopMixin


MOVED_METHODS = (
    "send",
    "_send_locked",
    "_send_locked_inner",
    "_get_codegraph_context",
    "_is_correction",
)


def test_session_inherits_mixin():
    assert issubclass(ConversationalSession, SendLoopMixin)
    assert SendLoopMixin in ConversationalSession.__mro__


def test_moved_methods_present_on_session():
    for name in MOVED_METHODS:
        assert hasattr(ConversationalSession, name), name
        attr = getattr(ConversationalSession, name)
        assert callable(attr), name


def test_moved_methods_resolve_to_mixin():
    for name in MOVED_METHODS:
        attr = getattr(ConversationalSession, name)
        assert attr.__qualname__ == f"SendLoopMixin.{name}", (
            name,
            attr.__qualname__,
        )


def test_mixin_defines_no_init():
    assert "__init__" not in SendLoopMixin.__dict__


def test_mixin_defines_no_owned_state_attrs():
    # No class-level instance state; methods only.
    owned = [
        k for k, v in SendLoopMixin.__dict__.items()
        if not k.startswith("__") and not callable(v) and not isinstance(v, (staticmethod, classmethod))
    ]
    assert owned == [], owned


def test_busy_control_not_folded_in():
    from harness.busy_control import BusyControlMixin

    for name in ("is_turn_busy", "interrupt", "_mark_busy_acquired", "_release_busy"):
        attr = getattr(ConversationalSession, name)
        assert attr.__qualname__ == f"BusyControlMixin.{name}", (
            name,
            attr.__qualname__,
        )


def test_tool_dispatch_not_folded_in():
    from harness.tool_dispatch import ToolDispatchMixin

    attr = getattr(ConversationalSession, "_do_read_file")
    assert attr.__qualname__ == "ToolDispatchMixin._do_read_file", attr.__qualname__


def test_mro_places_send_loop_before_busy_and_tools():
    mro = ConversationalSession.__mro__
    send_i = mro.index(SendLoopMixin)
    from harness.busy_control import BusyControlMixin
    from harness.tool_dispatch import ToolDispatchMixin

    assert send_i < mro.index(BusyControlMixin)
    assert send_i < mro.index(ToolDispatchMixin)
