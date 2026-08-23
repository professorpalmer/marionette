"""Correlation id threading for harness logs and HTTP responses."""

from __future__ import annotations

from harness.correlation import (
    correlation_scope,
    get_correlation_id,
    new_correlation_id,
    resolve_correlation_id,
    set_correlation_id,
)


def test_new_correlation_id_is_unique():
    a = new_correlation_id()
    b = new_correlation_id()
    assert a
    assert b
    assert a != b


def test_correlation_scope_restores_previous():
    with correlation_scope("outer"):
        assert get_correlation_id() == "outer"
        with correlation_scope("inner"):
            assert get_correlation_id() == "inner"
        assert get_correlation_id() == "outer"


def test_resolve_correlation_id_generates_when_missing():
    cid = resolve_correlation_id("")
    assert cid
    assert resolve_correlation_id(cid) == cid


def test_set_correlation_id_overrides_context():
    with correlation_scope("seed"):
        token = set_correlation_id("override")
        assert get_correlation_id() == "override"
        from harness.correlation import reset_correlation_id

        reset_correlation_id(token)
        assert get_correlation_id() == "seed"
