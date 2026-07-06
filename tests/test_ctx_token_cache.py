"""Tests for the per-turn context-token estimate cache in ConversationalSession.

Covers:
  (a) two consecutive _estimate_context_tokens() calls with unchanged history
      return the same value AND the second call does not re-walk the history
      (spy on _estimate_context_tokens_for_list to count invocations),
  (b) after appending a message (length changes) the estimate updates,
  (c) after an in-place same-length rebuild followed by _invalidate_ctx_cache()
      the estimate recomputes,
  (d) the cached value equals the uncached value.

Hermetic: no network, no driver, no filesystem side effects. Only stdlib.
"""

from harness.config import HarnessConfig
from harness.conversation import ConversationalSession


def _new_session() -> ConversationalSession:
    cfg = HarnessConfig(max_context_tokens=10000)
    s = ConversationalSession(cfg)
    s._history[0]["content"] = "sys"
    # Neutralize any real prompt-token metering so the heuristic path is
    # exercised deterministically -- the cache is on the heuristic.
    s._last_prompt_tokens = 0
    return s


class _CountingSpy:
    """Wrap the real _estimate_context_tokens_for_list and count calls."""

    def __init__(self, real):
        self.real = real
        self.calls = 0

    def __call__(self, history_list):
        self.calls += 1
        return self.real(history_list)


def test_second_call_returns_cached_and_does_not_rewalk():
    s = _new_session()
    s._history.append({"role": "user", "content": "hello world"})
    s._history.append({"role": "assistant", "content": "A" * 500})

    # Spy on the pure per-list estimator to count how many times the walk runs.
    real = s._estimate_context_tokens_for_list
    spy = _CountingSpy(real)
    s._estimate_context_tokens_for_list = spy  # type: ignore[assignment]

    first = s._estimate_context_tokens()
    calls_after_first = spy.calls
    second = s._estimate_context_tokens()
    calls_after_second = spy.calls

    assert first == second
    # First call must have walked the history at least once.
    assert calls_after_first >= 1
    # Second call must NOT have re-walked (cached fast-path).
    assert calls_after_second == calls_after_first


def test_appending_message_updates_estimate():
    s = _new_session()
    s._history.append({"role": "user", "content": "hi"})
    before = s._estimate_context_tokens()

    s._history.append({"role": "assistant", "content": "X" * 400})
    after = s._estimate_context_tokens()

    assert after > before


def test_inplace_same_length_rebuild_recomputes_after_invalidate():
    s = _new_session()
    s._history.append({"role": "user", "content": "small"})
    s._history.append({"role": "assistant", "content": "small too"})

    before = s._estimate_context_tokens()
    # Replace the LAST message with a much larger one, keeping length the same.
    assert len(s._history) >= 1
    original_len = len(s._history)
    s._history[-1] = {"role": "assistant", "content": "Y" * 2000}
    assert len(s._history) == original_len

    # Without invalidation, the len-keyed cache would stale-read the old value.
    stale = s._estimate_context_tokens()
    assert stale == before  # confirms the cache is doing its job (len unchanged)

    # After invalidation, the estimate must reflect the new content.
    s._invalidate_ctx_cache()
    fresh = s._estimate_context_tokens()
    assert fresh > before


def test_cached_value_equals_uncached_value():
    s = _new_session()
    s._history.append({"role": "user", "content": "the quick brown fox"})
    s._history.append({
        "role": "assistant",
        "content": "jumps over the lazy dog",
        "tool_calls": [
            {"id": "tc1", "function": {"name": "read_file", "arguments": '{"path":"a"}'}},
        ],
    })
    s._history.append({"role": "tool", "tool_call_id": "tc1", "content": "file contents here"})

    # Cached call.
    cached = s._estimate_context_tokens()

    # Force a fresh, uncached recompute by invalidating and calling again;
    # since _last_prompt_tokens == 0 the return equals the heuristic exactly.
    s._invalidate_ctx_cache()
    uncached = s._estimate_context_tokens()

    assert cached == uncached
    # And that must equal a direct heuristic computation over the same list.
    direct = s._estimate_context_tokens_for_list(s._history)
    assert cached == direct


def test_cache_survives_multiple_appends_and_stays_correct():
    """Length-keyed auto-invalidation on plain appends: each estimate must
    equal the fresh recompute, i.e. the cache never returns a stale value
    across length changes."""
    s = _new_session()
    for i in range(5):
        s._history.append({"role": "user", "content": f"msg {i} " * 10})
        cached = s._estimate_context_tokens()
        # Independent uncached recompute of the same list.
        direct = s._estimate_context_tokens_for_list(s._history)
        assert cached == direct


def test_last_prompt_tokens_max_still_applies_with_cache():
    """When _last_prompt_tokens > heuristic, the return must still be max(),
    identical to the pre-cache semantics."""
    s = _new_session()
    s._history.append({"role": "user", "content": "tiny"})
    heuristic = s._estimate_context_tokens_for_list(s._history)
    s._last_prompt_tokens = heuristic + 12345
    got = s._estimate_context_tokens()
    assert got == heuristic + 12345
    # And a repeated call is stable.
    assert s._estimate_context_tokens() == heuristic + 12345
