"""Smoke tests for the WikiDistillMixin extraction.

Guards the mechanical move of wiki grounding / ingest / distill helpers out of
harness.conversation into harness.wiki_distill. If the class-hierarchy wiring
or the MRO ever regresses, these fail loudly.
"""

from harness.conversation import ConversationalSession
from harness.wiki_distill import WikiDistillMixin


MOVED_METHODS = (
    "_wiki_grounding_query",
    "_build_turn_wiki_section",
    "_wiki_grounding_fields",
    "_after_wiki_ingest",
    "_maybe_ingest",
    "prepare_wiki_pages",
    "ingest_prepared_pages",
    "_build_transcript_digest",
    "_maybe_auto_distill",
    "distill",
)


def test_session_inherits_mixin():
    assert issubclass(ConversationalSession, WikiDistillMixin)
    assert WikiDistillMixin in ConversationalSession.__mro__


def test_moved_methods_present_on_session():
    for name in MOVED_METHODS:
        assert hasattr(ConversationalSession, name), name
        attr = getattr(ConversationalSession, name)
        assert callable(attr), name


def test_moved_methods_resolve_to_mixin():
    for name in MOVED_METHODS:
        attr = getattr(ConversationalSession, name)
        assert attr.__qualname__ == f"WikiDistillMixin.{name}", (
            name,
            attr.__qualname__,
        )


def test_mixin_defines_no_init():
    assert "__init__" not in WikiDistillMixin.__dict__


def test_wiki_grounding_max_chars_on_mixin():
    assert "_WIKI_GROUNDING_MAX_CHARS" in WikiDistillMixin.__dict__
    assert ConversationalSession._WIKI_GROUNDING_MAX_CHARS == 8000


def test_send_and_busy_not_folded_into_wiki_distill():
    # Busy / send live on their own mixins — not WikiDistillMixin.
    from harness.busy_control import BusyControlMixin
    from harness.send_loop import SendLoopMixin

    assert ConversationalSession.send.__qualname__ == "SendLoopMixin.send"
    assert (
        ConversationalSession._send_locked_inner.__qualname__
        == "SendLoopMixin._send_locked_inner"
    )
    assert ConversationalSession.interrupt.__qualname__ == "BusyControlMixin.interrupt"
