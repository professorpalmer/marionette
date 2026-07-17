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


def test_send_and_busy_stay_on_session():
    # Busy / send / _send_locked_inner must not have been swept into the mixin.
    for name in ("send", "_send_locked_inner"):
        attr = getattr(ConversationalSession, name)
        assert attr.__qualname__ == f"ConversationalSession.{name}", (
            name,
            attr.__qualname__,
        )
