"""coerce_token_usage shape coverage."""

from pmharness.drivers.token_usage import coerce_token_usage, coerce_token_usage_detail


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
    tin, tout, cost, cached = coerce_token_usage_detail(
        {
            "usage": {
                "input_tokens": 1000,
                "output_tokens": 40,
                "cache_read_input_tokens": 800,
            }
        }
    )
    assert (tin, tout, cost, cached) == (1000, 40, None, 800)


def test_coerce_cache_read_from_prompt_tokens_details():
    _tin, _tout, _cost, cached = coerce_token_usage_detail(
        {
            "usage": {
                "prompt_tokens": 500,
                "completion_tokens": 10,
                "prompt_tokens_details": {"cached_tokens": 400},
            }
        }
    )
    assert cached == 400
