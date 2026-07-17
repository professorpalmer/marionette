"""Smoke tests for the AdapterResolveMixin extraction.

Guards the mechanical move of nearly-pure adapter-resolve helpers out of
harness.conversation into harness.adapter_resolve. If the class-hierarchy
wiring or the MRO ever regresses, these fail loudly.
"""

from harness.conversation import ConversationalSession
from harness.adapter_resolve import AdapterResolveMixin


MOVED_METHODS = (
    "_external_adapter_available",
    "_validate_target_repo",
    "_resolve_requested_implement_adapter",
    "_active_adapters_system_note",
    "_detect_default_implement_adapter",
)


def test_session_inherits_mixin():
    assert issubclass(ConversationalSession, AdapterResolveMixin)
    # And the mixin appears in the MRO.
    assert AdapterResolveMixin in ConversationalSession.__mro__


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
        assert attr.__qualname__ == f"AdapterResolveMixin.{name}", (
            name,
            attr.__qualname__,
        )


def test_mixin_defines_no_init():
    # The mixin must not carry state or an __init__ of its own -- otherwise
    # it would interfere with ConversationalSession.__init__ via MRO.
    assert "__init__" not in AdapterResolveMixin.__dict__


def test_busy_send_swarm_remain_on_session():
    # Busy/send/swarm drain stay on ConversationalSession — not part of this peel.
    for name in ("_send_locked", "_send_locked_inner", "_await_and_apply_job"):
        assert hasattr(ConversationalSession, name), name
        attr = getattr(ConversationalSession, name)
        assert attr.__qualname__ == f"ConversationalSession.{name}", (
            name,
            attr.__qualname__,
        )
