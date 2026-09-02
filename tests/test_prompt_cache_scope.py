from __future__ import annotations

"""Builder-declared prompt-cache watermarks and rotation-stable scope."""

import hashlib

from harness.prompt_cache_scope import (
    clear_stable_prefixes,
    find_stable_prefix,
    prompt_cache_scope,
    register_stable_prefix,
)


def setup_function():
    clear_stable_prefixes()


def teardown_function():
    clear_stable_prefixes()


def test_prompt_cache_scope_is_stable_sha256():
    key = "lineage-root-alpha"
    first = prompt_cache_scope(key)
    second = prompt_cache_scope(key)
    assert first == second
    assert first == hashlib.sha256(key.encode("utf-8")).hexdigest()
    assert prompt_cache_scope("lineage-root-beta") != first


def test_prompt_cache_scope_never_embeds_session_id():
    session_id = "sess-physical-abc123-do-not-leak"
    scope = prompt_cache_scope("compression-lineage-ROOT")
    assert session_id not in scope
    assert scope != session_id
    hashed_if_misused = prompt_cache_scope(session_id)
    assert hashed_if_misused != session_id
    assert session_id not in hashed_if_misused


def test_register_and_find_stable_prefix_longest_match():
    register_stable_prefix("short", "You are")
    register_stable_prefix("system_v1", "You are Marionette.\n")
    register_stable_prefix("", "ignored-empty-name")
    assert find_stable_prefix("You are Marionette.\nUser turn") == "system_v1"
    assert find_stable_prefix("Hello") is None
    assert find_stable_prefix("") is None
