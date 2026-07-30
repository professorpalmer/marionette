"""Canonical model identity: no agentic/agentic/... envelopes."""
from __future__ import annotations

from harness.model_identity import (
    collapse_engine_prefixes,
    envelope_model_id,
    filter_rejected_excluding_selected,
    format_model_ref,
    model_ids_equal,
    price_lookup_id,
    strip_engine_prefixes,
)


def test_strip_engine_prefixes_is_idempotent_for_double_prefix():
    assert strip_engine_prefixes("agentic/agentic/deepseek/deepseek-v4-pro") == (
        "deepseek/deepseek-v4-pro"
    )
    assert strip_engine_prefixes("agentic/deepseek/deepseek-v4-pro") == (
        "deepseek/deepseek-v4-pro"
    )
    assert strip_engine_prefixes("native/stub-oracle-v2") == "stub-oracle-v2"


def test_collapse_and_envelope_never_double_prefix():
    assert collapse_engine_prefixes("agentic/agentic/deepseek/deepseek-v4-pro") == (
        "agentic/deepseek/deepseek-v4-pro"
    )
    assert envelope_model_id("agentic", "agentic/deepseek/deepseek-v4-pro") == (
        "agentic/deepseek/deepseek-v4-pro"
    )
    assert envelope_model_id("agentic", "agentic/agentic/deepseek/deepseek-v4-pro") == (
        "agentic/deepseek/deepseek-v4-pro"
    )
    # Legacy bare provider/slug still gets one engine prefix.
    assert envelope_model_id("agentic", "z-ai/glm-5.2") == "agentic/z-ai/glm-5.2"
    assert envelope_model_id("native", "stub-oracle-v2") == "native/stub-oracle-v2"


def test_price_lookup_strips_all_engine_prefixes():
    assert price_lookup_id("agentic/agentic/deepseek/deepseek-v4-pro") == (
        "deepseek/deepseek-v4-pro"
    )


def test_model_ids_equal_across_prefix_shapes():
    assert model_ids_equal(
        "agentic/deepseek/deepseek-v4-pro",
        "deepseek/deepseek-v4-pro",
    )
    assert model_ids_equal(
        "agentic/agentic/deepseek/deepseek-v4-pro",
        "agentic/deepseek/deepseek-v4-pro",
    )
    assert not model_ids_equal(
        "agentic/deepseek/deepseek-v4-pro",
        "agentic/deepseek/deepseek-v4-flash",
    )


def test_filter_rejected_excludes_selected_under_identity():
    rejected = [
        {"model": "agentic/deepseek/deepseek-v4-pro", "reason": "more expensive"},
        {"model": "agentic/deepseek/deepseek-v4-flash", "reason": "weaker"},
        {"id": "deepseek/deepseek-v4-pro", "reason": "duplicate bare"},
    ]
    out = filter_rejected_excluding_selected(
        rejected, "agentic/deepseek/deepseek-v4-pro",
    )
    assert [r["model"] for r in out] == ["agentic/deepseek/deepseek-v4-flash"]


def test_format_model_ref_fields():
    ref = format_model_ref("agentic", "agentic/agentic/deepseek/deepseek-v4-pro")
    assert ref["display_id"] == "agentic/deepseek/deepseek-v4-pro"
    assert ref["price_id"] == "deepseek/deepseek-v4-pro"
    assert ref["engine"] == "agentic"
