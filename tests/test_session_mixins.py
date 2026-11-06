"""ConversationalSession is composed from mixins. Two rules keep that composition safe."""

from harness.conversation import ConversationalSession

_MIXINS = ConversationalSession.__mro__[1:-1]


def test_only_the_session_defines_init():
    # A mixin __init__ would run (or be skipped) by MRO order, not by intent.
    assert [c.__name__ for c in _MIXINS if "__init__" in c.__dict__] == []


def test_no_method_name_is_defined_on_two_classes():
    # With unique names the MRO order cannot change which method runs.
    owners = {}
    for cls in (ConversationalSession,) + _MIXINS:
        for name, value in cls.__dict__.items():
            if not name.startswith("__") and callable(value):
                owners.setdefault(name, []).append(cls.__name__)
    assert {n: c for n, c in owners.items() if len(c) > 1} == {}
