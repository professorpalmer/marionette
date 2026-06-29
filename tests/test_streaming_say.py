"""Tests for StreamingSayExtractor -- real-time prose extraction from a streaming
pilot JSON envelope so text renders token-by-token instead of dumping at once."""
from harness.pilot import StreamingSayExtractor


def _run(chunks):
    ex = StreamingSayExtractor()
    return "".join(ex.feed(c) for c in chunks)


def test_char_by_char_clean_envelope():
    full = '{"say": "Sure, let me take a look.", "actions": [{"kind":"run_command"}]}'
    assert _run(list(full)) == "Sure, let me take a look."


def test_chunked_envelope():
    chunks = ['{"say": "Sure, ', 'let me ', 'take a look.", "actions": []}']
    assert _run(chunks) == "Sure, let me take a look."


def test_escapes_decoded():
    # \n and escaped quotes inside the say value are decoded to real chars.
    full = '{"say": "Line one\\nLine \\"two\\"", "actions": []}'
    assert _run(list(full)) == 'Line one\nLine "two"'


def test_unicode_escape_decoded():
    full = '{"say": "smart \\u2014 dash", "actions": []}'
    assert _run(list(full)) == "smart \u2014 dash"


def test_bare_prose_streams_verbatim():
    assert _run(list("Just talking, no JSON.")) == "Just talking, no JSON."


def test_thinking_before_say_not_leaked():
    full = '{"thinking": "I should inspect text files", "say": "Done.", "actions": []}'
    assert _run(list(full)) == "Done."


def test_say_value_containing_actions_word():
    full = '{"say": "I will run actions now: ok?", "actions": []}'
    assert _run(list(full)) == "I will run actions now: ok?"


def test_say_closes_only_on_unescaped_quote():
    full = '{"say": "He said \\"hi\\" to me", "actions": []}'
    assert _run(list(full)) == 'He said "hi" to me'


def test_message_key_alias():
    full = '{"message": "Via message key.", "actions": []}'
    assert _run(list(full)) == "Via message key."


def test_empty_and_no_say():
    assert _run([""]) == ""
    # Envelope with only actions, no say -> nothing streamed.
    assert _run(list('{"actions": []}')) == ""
