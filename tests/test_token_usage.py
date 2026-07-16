"""coerce_token_usage shape coverage."""

from pmharness.drivers.token_usage import coerce_token_usage


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
