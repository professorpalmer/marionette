"""Smoke tests for the ToolDispatchMixin extraction.

Guards the mechanical move of `_do_*` per-tool handlers out of
harness.conversation into harness.tool_dispatch. If the class-hierarchy
wiring or the MRO ever regresses, these fail loudly.
"""

from harness.conversation import ConversationalSession
from harness.tool_dispatch import ToolDispatchMixin


MOVED_METHODS = (
    "_do_read_file",
    "_do_view_image",
    "_do_list_dir",
    "_do_lsp",
    "_do_web_search",
    "_do_web_fetch",
    "_do_read_pdf",
    "_do_search_codegraph",
    "_do_search_files",
    "_do_search_tools",
    "_do_hash_edit",
)


def test_session_inherits_mixin():
    assert issubclass(ConversationalSession, ToolDispatchMixin)
    # And the mixin appears in the MRO.
    assert ToolDispatchMixin in ConversationalSession.__mro__


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
        assert attr.__qualname__ == f"ToolDispatchMixin.{name}", (
            name,
            attr.__qualname__,
        )


def test_mixin_defines_no_init():
    # The mixin must not carry state or an __init__ of its own -- otherwise
    # it would interfere with ConversationalSession.__init__ via MRO.
    assert "__init__" not in ToolDispatchMixin.__dict__


def test_reexported_helpers_still_importable_from_conversation():
    # Callers that historically imported these from harness.conversation
    # keep working after the move.
    from harness.conversation import is_safe_path, _strip_ansi, _ANSI_ESCAPE  # noqa: F401
    assert callable(is_safe_path)
    assert callable(_strip_ansi)
