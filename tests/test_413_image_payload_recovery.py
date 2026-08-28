"""HTTP 413 is a byte-size error. Token heuristics undercount data-URL images."""

from harness.compaction_mixin import CompactionContextMixin
from harness.context_budget import age_history_images, serialized_history_bytes


def _huge_data_url(n_bytes=80_000):
    return "data:image/png;base64," + ("A" * n_bytes)


def _image_part(url):
    return {"type": "image_url", "image_url": {"url": url}}


def _history_with_stale_image(payload_bytes=80_000):
    return [
        {"role": "system", "content": "sys"},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "see this older shot"},
                _image_part(_huge_data_url(payload_bytes)),
            ],
        },
        {"role": "assistant", "content": "noted"},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "and this live one"},
                _image_part("data:image/png;base64,LIVE"),
            ],
        },
    ]


def _flat_per_image_token_estimate(messages):
    """Same cheap clock compaction uses: list content is priced as len(parts)."""
    mixin = CompactionContextMixin.__new__(CompactionContextMixin)
    return mixin._estimate_context_tokens_for_list(messages)


def test_serialized_history_bytes_never_raises_on_bad_input():
    assert serialized_history_bytes(None) == 0
    assert serialized_history_bytes("not-a-list") == 0
    assert serialized_history_bytes(object()) == 0
    assert serialized_history_bytes([]) == 2  # "[]"


def test_huge_data_url_bytes_dwarf_token_heuristic():
    history = _history_with_stale_image()
    nbytes = serialized_history_bytes(history)
    tokens = _flat_per_image_token_estimate(history)
    assert nbytes > 80_000
    assert nbytes > tokens * 20


def test_age_history_images_drops_bytes_token_estimate_barely_moves():
    history = _history_with_stale_image()
    original_url = history[1]["content"][1]["image_url"]["url"]
    before_bytes = serialized_history_bytes(history)
    before_tokens = _flat_per_image_token_estimate(history)

    aged = age_history_images(history, keep_last_user=True)

    after_bytes = serialized_history_bytes(aged)
    after_tokens = _flat_per_image_token_estimate(aged)

    assert after_bytes < before_bytes
    assert before_bytes - after_bytes > 70_000
    # Replacing image_url with a short text part barely moves chars//4.
    assert abs(after_tokens - before_tokens) <= 20
    # Overflow recovery treats a byte drop as progress (not token equality).
    assert after_bytes < before_bytes

    # Copies only — shared fixtures keep their payloads.
    assert history[1]["content"][1]["type"] == "image_url"
    assert history[1]["content"][1]["image_url"]["url"] == original_url
    assert aged[1]["content"][1]["type"] == "text"
    assert aged[1]["content"][1]["text"] == "[image removed during compaction]"
    assert any(
        isinstance(p, dict) and p.get("type") == "image_url"
        for p in aged[-1]["content"]
    )


def test_age_history_images_exception_does_not_wipe(monkeypatch):
    history = _history_with_stale_image(payload_bytes=1000)

    def _boom(*_a, **_k):
        raise RuntimeError("copy fail")

    monkeypatch.setattr("harness.context_budget.copy.deepcopy", _boom)
    aged = age_history_images(history, keep_last_user=True)
    assert len(aged) == len(history)
    assert aged[1]["content"][1]["type"] == "image_url"


def test_age_history_images_can_strip_last_user_when_asked():
    history = _history_with_stale_image(payload_bytes=1000)
    aged = age_history_images(history, keep_last_user=False)
    for msg in aged:
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        assert all(
            not (isinstance(p, dict) and p.get("type") == "image_url")
            for p in content
        )
