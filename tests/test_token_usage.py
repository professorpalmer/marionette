"""coerce_token_usage shape coverage."""

from pmharness.drivers.token_usage import (
    ModalityBucket,
    attach_modality_fields,
    coerce_token_usage,
    coerce_token_usage_detail,
    coerce_token_usage_record,
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


def test_openai_reasoning_tokens_extract_only():
    usage = {
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "completion_tokens_details": {"reasoning_tokens": 42},
        }
    }
    tin, tout, cost = coerce_token_usage(usage)
    detail = coerce_token_usage_record(usage)
    assert (tin, tout, cost) == (100, 50, None)
    assert detail.reasoning_tokens == ModalityBucket(basis="provider", count=42)
    assert detail.image_tokens.basis == "absent"
    assert detail.modality_dict() == {
        "reasoning_tokens": 42,
        "reasoning_tokens_basis": "provider",
    }


def test_openai_image_tokens_in_prompt_details():
    usage = {
        "usage": {
            "prompt_tokens": 200,
            "completion_tokens": 10,
            "prompt_tokens_details": {"image_tokens": 128, "cached_tokens": 50},
        }
    }
    tin, tout, _cost, cached, _write = coerce_token_usage_detail(usage)
    detail = coerce_token_usage_record(usage)
    assert (tin, tout, cached) == (200, 10, 50)
    assert detail.image_tokens == ModalityBucket(basis="provider", count=128)
    assert detail.cached_tokens_detail == ModalityBucket(basis="provider", count=50)
    meta: dict = {}
    attach_modality_fields(meta, detail)
    assert meta["image_tokens"] == 128
    assert meta["cached_tokens_detail"] == 50
    assert "encrypted_opaque_tokens" not in meta


def test_codex_responses_modality_shapes():
    usage = {
        "usage": {
            "input_tokens": 1000,
            "output_tokens": 500,
            "input_tokens_details": {
                "cached_tokens": 800,
                "encrypted_content_tokens": 120,
            },
            "output_tokens_details": {"reasoning_tokens": 300},
        }
    }
    detail = coerce_token_usage_record(usage)
    assert detail.tokens_in == 1000
    assert detail.tokens_out == 500
    assert detail.cache_read == 800
    assert detail.reasoning_tokens == ModalityBucket(basis="provider", count=300)
    assert detail.encrypted_opaque_tokens == ModalityBucket(basis="provider", count=120)
    assert detail.cached_tokens_detail == ModalityBucket(basis="provider", count=800)


def test_codex_reasoning_output_tokens_top_level():
    detail = coerce_token_usage_record(
        {"usage": {"input_tokens": 10, "output_tokens": 5, "reasoning_output_tokens": 3}}
    )
    assert detail.reasoning_tokens == ModalityBucket(basis="provider", count=3)
    assert (detail.tokens_in, detail.tokens_out) == (10, 5)


def test_anthropic_usage_does_not_invent_modalities():
    detail = coerce_token_usage_record(
        {
            "usage": {
                "input_tokens": 100,
                "output_tokens": 20,
                "cache_read_input_tokens": 5_000,
                "cache_creation_input_tokens": 200,
            }
        }
    )
    assert detail.tokens_in == 100 + 5_000 + 200
    assert detail.reasoning_tokens.basis == "absent"
    assert detail.image_tokens.basis == "absent"
    assert detail.encrypted_opaque_tokens.basis == "absent"
    assert detail.modality_dict() == {}


def test_cursor_shape_does_not_invent_modalities():
    detail = coerce_token_usage_record(
        {
            "usage": {
                "inputTokens": 7,
                "outputTokens": 412,
                "cacheReadTokens": 147_695,
                "cacheWriteTokens": 39_331,
            }
        }
    )
    assert detail.tokens_in == 7 + 147_695 + 39_331
    assert detail.reasoning_tokens.basis == "absent"
    assert detail.modality_dict() == {}


def test_tuple_api_unchanged_with_modalities_present():
    usage = {
        "usage": {
            "prompt_tokens": 10,
            "completion_tokens": 3,
            "cost": 0.02,
            "completion_tokens_details": {"reasoning_tokens": 99},
        }
    }
    assert coerce_token_usage_detail(usage) == (10, 3, 0.02, 0, 0)
    assert coerce_token_usage(usage) == (10, 3, 0.02)


def test_provider_reported_zero_reasoning_is_preserved():
    detail = coerce_token_usage_record(
        {"usage": {"prompt_tokens": 1, "completion_tokens_details": {"reasoning_tokens": 0}}}
    )
    assert detail.reasoning_tokens == ModalityBucket(basis="provider", count=0)
    assert detail.modality_dict()["reasoning_tokens"] == 0


def test_modality_rejects_bool_negative_fractional_and_non_finite():
    def _reasoning(usage):
        return coerce_token_usage_record(usage).reasoning_tokens

    absent = ModalityBucket.absent()
    assert _reasoning({"usage": {"prompt_tokens": 1, "completion_tokens_details": {"reasoning_tokens": True}}}) == absent
    assert _reasoning({"usage": {"prompt_tokens": 1, "completion_tokens_details": {"reasoning_tokens": False}}}) == absent
    assert _reasoning({"usage": {"prompt_tokens": 1, "completion_tokens_details": {"reasoning_tokens": -1}}}) == absent
    assert _reasoning({"usage": {"prompt_tokens": 1, "completion_tokens_details": {"reasoning_tokens": 3.5}}}) == absent
    assert _reasoning({"usage": {"prompt_tokens": 1, "completion_tokens_details": {"reasoning_tokens": float("nan")}}}) == absent
    assert _reasoning({"usage": {"prompt_tokens": 1, "completion_tokens_details": {"reasoning_tokens": float("inf")}}}) == absent
    # Legacy billed tuple unchanged when modality fields are malformed.
    usage = {
        "usage": {
            "prompt_tokens": 10,
            "completion_tokens": 3,
            "cost": 0.02,
            "completion_tokens_details": {"reasoning_tokens": True},
        }
    }
    assert coerce_token_usage_detail(usage) == (10, 3, 0.02, 0, 0)
    assert coerce_token_usage(usage) == (10, 3, 0.02)
    assert coerce_token_usage_record(usage).modality_dict() == {}
