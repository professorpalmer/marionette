"""coerce_token_usage shape coverage."""

from pmharness.drivers.token_usage import (
    coerce_token_usage,
    coerce_token_usage_detail,
    expand_uncached_prompt_tokens,
)


def test_coerce_openai_style():
    tin, tout, cost = coerce_token_usage(
        {"usage": {"prompt_tokens": 10, "completion_tokens": 3, "cost": 0.02}}
    )
    assert (tin, tout, cost) == (10, 3, 0.02)


def test_coerce_acp_camel_case():
    tin, tout, cost = coerce_token_usage(
        {"usage": {"inputTokens": 120, "outputTokens": 8}}
    )
    assert (tin, tout) == (120, 8)
    assert cost is None


def test_later_blob_wins_nonzero():
    tin, tout, _ = coerce_token_usage(
        {"usage": {"input_tokens": 1, "output_tokens": 1}},
        {"usage": {"input_tokens": 99, "output_tokens": 7}},
    )
    assert (tin, tout) == (99, 7)


def test_coerce_cache_read_from_cursor_cli_shape():
    tin, tout, cost, cached, write = coerce_token_usage_detail(
        {
            "usage": {
                "input_tokens": 1000,
                "output_tokens": 40,
                "cache_read_input_tokens": 800,
            }
        }
    )
    # OpenAI/Anthropic-subset style: cached <= input → leave tin alone.
    assert (tin, tout, cost, cached, write) == (1000, 40, None, 800, 0)


def test_coerce_cache_read_from_prompt_tokens_details():
    _tin, _tout, _cost, cached, _write = coerce_token_usage_detail(
        {
            "usage": {
                "prompt_tokens": 500,
                "completion_tokens": 10,
                "prompt_tokens_details": {"cached_tokens": 400},
            }
        }
    )
    assert cached == 400


def test_expand_uncached_prompt_tokens_cursor_cli_semantics():
    # Cursor forum: inputTokens is uncached only; dashboard Tokens =
    # input + cacheRead + cacheWrite (+ output separately).
    full, cached, write = expand_uncached_prompt_tokens(7, 147_695, 39_331)
    assert full == 7 + 147_695 + 39_331
    assert (cached, write) == (147_695, 39_331)


def test_expand_leaves_openai_full_prompt_alone():
    full, cached, write = expand_uncached_prompt_tokens(1000, 800, 0)
    assert (full, cached, write) == (1000, 800, 0)


def test_coerce_cursor_cli_uncached_plus_cache_buckets():
    tin, tout, cost, cached, write = coerce_token_usage_detail(
        {
            "usage": {
                "inputTokens": 7,
                "outputTokens": 412,
                "cacheReadTokens": 147_695,
                "cacheWriteTokens": 39_331,
            }
        }
    )
    assert tin == 7 + 147_695 + 39_331
    assert tout == 412
    assert cost is None
    assert cached == 147_695
    assert write == 39_331


def test_coerce_nested_result_usage():
    tin, tout, _cost, cached, write = coerce_token_usage_detail(
        {
            "result": {
                "usage": {
                    "inputTokens": 3,
                    "outputTokens": 9,
                    "cacheReadTokens": 50_000,
                    "cacheWriteTokens": 1_000,
                }
            }
        }
    )
    assert tin == 3 + 50_000 + 1_000
    assert tout == 9
    assert cached == 50_000
    assert write == 1_000


def test_coerce_anthropic_uncached_plus_cache_creation():
    tin, tout, _cost, cached, write = coerce_token_usage_detail(
        {
            "usage": {
                "input_tokens": 100,
                "output_tokens": 20,
                "cache_read_input_tokens": 5_000,
                "cache_creation_input_tokens": 200,
            }
        }
    )
    assert tin == 100 + 5_000 + 200
    assert (cached, write) == (5_000, 200)
    assert tout == 20
