from __future__ import annotations

from harness.compaction_vault import (
    VAULT_HEADING,
    build_plan_recap_chunk,
    build_turn_vault_section,
    drop_ack_like_messages,
    index_elided_messages,
    is_recap_ask,
    retrieve_vault_chunks,
    retrieve_vault_result,
    vault_match_query,
)


def test_vault_match_query_is_or_not_and():
    expr = vault_match_query("What write constraint was set for production.db?")
    assert "OR" in expr
    assert "production.db" in expr
    assert "AND" not in expr


def test_vault_retrieve_recalls_elided_nonce(tmp_path):
    messages = [
        {"role": "user", "content": "Run the cache probe."},
        {
            "role": "tool",
            "content": "Plain measurement only: omega-cache-token-9f3a observed.",
        },
        {"role": "assistant", "content": "Cache probe finished."},
    ]
    written = index_elided_messages(str(tmp_path), "sess-vault", messages)
    assert written >= 2
    hits = retrieve_vault_chunks(
        str(tmp_path),
        "sess-vault",
        "What cache-shard measurement tokens were returned by the probe?",
    )
    assert any("omega-cache-token-9f3a" in hit for hit in hits)
    section = build_turn_vault_section(
        str(tmp_path),
        "sess-vault",
        "What cache-shard measurement tokens were returned by the probe?",
    )
    assert VAULT_HEADING in section
    assert "omega-cache-token-9f3a" in section


def test_vault_retrieve_is_session_scoped(tmp_path):
    index_elided_messages(
        str(tmp_path),
        "sess-a",
        [{"role": "user", "content": "never write to production.db"}],
    )
    index_elided_messages(
        str(tmp_path),
        "sess-b",
        [{"role": "user", "content": "omega-cache-token-9f3a"}],
    )
    hits = retrieve_vault_chunks(
        str(tmp_path),
        "sess-a",
        "What early write constraint was set for the database files?",
    )
    assert any("production.db" in hit for hit in hits)
    assert not any("omega-cache-token-9f3a" in hit for hit in hits)


def test_vault_recalls_plain_measurement_nonce(tmp_path):
    messages = [
        {"role": "user", "content": "Run the cache probe."},
        {
            "role": "tool",
            "content": "Plain measurement only: omega-cache-token-9f3a shard-omega-p95.",
        },
        {"role": "assistant", "content": "Cache probe finished."},
    ]
    written = index_elided_messages(str(tmp_path), "sess-miss", messages)
    assert written >= 1
    section = build_turn_vault_section(
        str(tmp_path),
        "sess-miss",
        "What cache-shard measurement tokens were returned by the probe?",
    )
    lowered = section.lower()
    assert "omega-cache-token-9f3a" in lowered
    assert "shard-omega-p95" in lowered


def test_vault_only_prose_selected_story_and_vault_hit(tmp_path):
    from harness.compaction_residual import build_catalog_residual

    messages = [
        {"role": "system", "content": "You are a coding assistant in a long session."},
        {
            "role": "user",
            "content": (
                "The billing cutoff is the fourteenth of each month "
                "for the omega ledger close."
            ),
        },
        {"role": "assistant", "content": "Noted the close date."},
    ]
    catalog = build_catalog_residual(messages, char_budget=4000)
    assert "fourteenth of each month" in catalog.lower()
    written = index_elided_messages(str(tmp_path), "sess-prose", messages)
    assert written >= 1
    section = build_turn_vault_section(
        str(tmp_path),
        "sess-prose",
        "When is the billing cutoff for the ledger close?",
    )
    assert "fourteenth of each month" in section.lower()


def test_vault_recap_paraphrase_and_twin_routes(tmp_path):
    from harness.compaction_residual import build_catalog_residual

    narrative = [
        {"role": "user", "content": "The canary ships to the spare region before Friday."},
        {"role": "assistant", "content": "Noted the ship plan."},
    ]
    paraphrase = [
        {"role": "user", "content": "The omega ledger close uses cutoff day twenty-seven."},
        {"role": "assistant", "content": "Noted the close day."},
    ]
    twin = [
        {"role": "user", "content": "The canary ships to the primary region."},
        {"role": "assistant", "content": "Recorded the first ship plan."},
        {"role": "user", "content": "The canary now ships to the spare region."},
        {"role": "assistant", "content": "Recorded the replacement ship plan."},
    ]
    catalog = build_catalog_residual(narrative, char_budget=4000)
    assert "spare region" in catalog.lower()
    index_elided_messages(str(tmp_path), "sess-narr", narrative)
    index_elided_messages(str(tmp_path), "sess-para", paraphrase)
    index_elided_messages(str(tmp_path), "sess-twin", twin)

    recap_q = vault_match_query("Remind me what we decided earlier.")
    assert "spare" not in recap_q.lower()
    assert is_recap_ask("Remind me what we decided earlier.") is True
    assert "Remind" in recap_q or "decided" in recap_q
    plan = build_plan_recap_chunk(narrative)
    assert "spare region" in plan.lower()
    narr = retrieve_vault_result(
        str(tmp_path), "sess-narr", "Remind me what we decided earlier."
    )
    assert narr["route"] == "recap_plan"
    assert "spare region" in "\n".join(narr["hits"]).lower()

    para_q = vault_match_query("When do invoices freeze?")
    assert "twenty" not in para_q.lower()
    para = retrieve_vault_result(
        str(tmp_path), "sess-para", "When do invoices freeze?"
    )
    assert para["route"] == "empty"
    assert para["hits"] == []

    twin_hits = retrieve_vault_chunks(
        str(tmp_path), "sess-twin", "Where does the canary ship?"
    )
    twin_blob = "\n".join(twin_hits).lower()
    assert "spare region" in twin_blob
    assert "primary region" not in twin_blob


def test_vault_survives_peek_archive_middle_eviction(tmp_path):
    import json

    from harness.compaction_archive import retain_archive_messages

    buried = (
        "The billing cutoff is the fourteenth of each month "
        "for the omega ledger close."
    )
    pad = "x" * 800
    prefix = [{"role": "user", "content": f"pre {i} {pad}"} for i in range(200)]
    mid = [
        {"role": "user", "content": buried},
        {"role": "assistant", "content": "Noted the close date."},
    ]
    suffix = [{"role": "user", "content": f"post {i} {pad}"} for i in range(200)]
    messages = prefix + mid + suffix
    retained = retain_archive_messages(messages)
    assert "fourteenth" not in json.dumps(retained).lower()
    written = index_elided_messages(str(tmp_path), "sess-evict", messages)
    assert written >= 1
    section = build_turn_vault_section(
        str(tmp_path),
        "sess-evict",
        "When is the billing cutoff for the ledger close?",
    )
    assert "fourteenth of each month" in section.lower()


def test_plan_chunk_skips_injected_residuals():
    """Compact residuals are role=user; they must not become the next plan."""
    plan = build_plan_recap_chunk([
        {"role": "user", "content": "The canary ships to the primary region."},
        {
            "role": "user",
            "_compressed_summary": True,
            "content": (
                "[Earlier conversation summarized to fit context]\n"
                "### Last ask\n"
                "- The canary now ships to the spare region."
            ),
        },
    ])
    assert "primary region" in plan.lower()
    assert "spare region" not in plan.lower()


def test_topic_last_wins_drops_superseded_obligation(tmp_path):
    reversal = [
        {
            "role": "user",
            "content": (
                "please don't write to the live ledger; "
                "the east replica is the only sink."
            ),
        },
        {"role": "assistant", "content": "Noted."},
        {
            "role": "user",
            "content": (
                "go ahead and write to the live ledger now; "
                "the east replica is retired."
            ),
        },
        {"role": "assistant", "content": "Reversed."},
    ]
    plan = build_plan_recap_chunk(reversal)
    low = plan.lower()
    assert "write to the live ledger now" in low
    assert "east replica is retired" in low
    assert "don't write" not in low
    assert "only sink" not in low
    assert "reversed." not in low

    twin = [
        {"role": "user", "content": "The canary ships to the primary region."},
        {"role": "assistant", "content": "Recorded the first ship plan."},
        {"role": "user", "content": "The canary now ships to the spare region."},
        {"role": "assistant", "content": "Recorded the replacement ship plan."},
    ]
    twin_plan = build_plan_recap_chunk(twin)
    assert "spare region" in twin_plan.lower()
    assert "primary region" not in twin_plan.lower()

    kept = [
        {
            "role": "user",
            "content": (
                "please don't write to the live ledger; "
                "the east replica is the only sink."
            ),
        },
        {"role": "assistant", "content": "Noted."},
    ]
    kept_plan = build_plan_recap_chunk(kept)
    assert "don't write" in kept_plan.lower() or "live ledger" in kept_plan.lower()

    index_elided_messages(str(tmp_path), "sess-rev", reversal)
    rev_hit = retrieve_vault_result(
        str(tmp_path),
        "sess-rev",
        "What is the current live-ledger write policy?",
    )
    rev_blob = "\n".join(rev_hit["hits"]).lower()
    assert "write to the live ledger now" in rev_blob
    assert "don't write" not in rev_blob
    assert "only sink" not in rev_blob

    index_elided_messages(str(tmp_path), "sess-keep", kept)
    keep_hit = retrieve_vault_result(
        str(tmp_path),
        "sess-keep",
        "What write sink is allowed, and what must not be written?",
    )
    keep_blob = "\n".join(keep_hit["hits"]).lower()
    assert "live ledger" in keep_blob


def test_vault_selector_default_worthy_contract(tmp_path):
    """Last-N story, no miss_plan, tighter recap. These used to fail."""
    from harness.compaction_vault import _filler_like

    leak = [
        {"role": "user", "content": "The canary ships to the spare region before Friday."},
        {"role": "assistant", "content": "Noted the ship plan."},
        {"role": "user", "content": "please update the changelog tone to be warmer."},
        {"role": "assistant", "content": "recorded."},
    ]
    leak_plan = build_plan_recap_chunk(leak)
    assert "spare region" in leak_plan.lower()
    assert "warmer" in leak_plan.lower()

    assert not _filler_like(
        "please keep this docs only: ship the canary to the spare "
        "region before Friday."
    )
    kept = [
        {
            "role": "user",
            "content": (
                "please keep this docs only: ship the canary to the spare "
                "region before Friday."
            ),
        },
        {"role": "assistant", "content": "Noted the ship plan."},
    ]
    kept_plan = build_plan_recap_chunk(kept)
    assert "spare region" in kept_plan.lower()
    index_elided_messages(str(tmp_path), "sess-docs", kept)
    kept_hit = retrieve_vault_result(
        str(tmp_path), "sess-docs", "Remind me what we decided earlier."
    )
    assert kept_hit["route"] == "recap_plan"
    assert "spare region" in "\n".join(kept_hit["hits"]).lower()

    notes = []
    for i in range(12):
        notes.append({
            "role": "user",
            "content": f"cap note {i}: please update the changelog tone to be warmer.",
        })
        notes.append({"role": "assistant", "content": f"cap note {i}: recorded."})
    capped = (
        [{"role": "user", "content": "The canary ships to the primary region."},
         {"role": "assistant", "content": "Recorded the first ship plan."}]
        + notes
        + [{"role": "user", "content": "The canary now ships to the spare region."},
           {"role": "assistant", "content": "Recorded the replacement ship plan."}]
    )
    capped_plan = build_plan_recap_chunk(capped)
    assert "spare region" in capped_plan.lower()
    assert "primary region" not in capped_plan.lower()

    assistant = [
        {"role": "user", "content": "What should we do about the canary?"},
        {"role": "assistant", "content": "Ship the canary to the spare region before Friday."},
    ]
    assistant_plan = build_plan_recap_chunk(assistant)
    assert "spare region" in assistant_plan.lower()

    wrong = [
        {"role": "user", "content": "The canary ships to the spare region before Friday."},
        {"role": "assistant", "content": "Noted the ship plan."},
    ]
    index_elided_messages(str(tmp_path), "sess-wrong", wrong)
    wrong_hit = retrieve_vault_result(
        str(tmp_path), "sess-wrong", "When do invoices freeze?"
    )
    assert wrong_hit["route"] == "empty"
    assert wrong_hit["hits"] == []

    assert is_recap_ask("Can you remind the test runner to skip flaky peek?") is False
    index_elided_messages(str(tmp_path), "sess-fire", wrong)
    fire_hit = retrieve_vault_result(
        str(tmp_path),
        "sess-fire",
        "Can you remind the test runner to skip flaky peek?",
    )
    assert fire_hit["route"] != "recap_plan"
    assert "spare region" not in "\n".join(fire_hit["hits"]).lower()


def test_drop_ack_like_messages_skips_reversed():
    kept = drop_ack_like_messages([
        {"role": "user", "content": "go ahead and write to the live ledger now"},
        {"role": "assistant", "content": "Reversed."},
        {"role": "assistant", "content": "Write policy recorded in the ledger notes."},
        {
            "role": "assistant",
            "content": "Noted.",
            "tool_calls": [{"id": "c1", "function": {"name": "read_file"}}],
        },
    ])
    texts = [row["content"] for row in kept]
    assert "Reversed." not in texts
    assert "go ahead and write to the live ledger now" in texts
    assert "Write policy recorded in the ledger notes." in texts
    assert any(row.get("tool_calls") for row in kept)


def test_vault_redacts_secrets(tmp_path):
    index_elided_messages(
        str(tmp_path),
        "sess-secret",
        [{"role": "tool", "content": "failed with token sk-abcdefghijklmnopqrstuvwx"}],
    )
    hits = retrieve_vault_chunks(
        str(tmp_path),
        "sess-secret",
        "What token failed?",
    )
    blob = "\n".join(hits)
    assert "sk-abcdefghijklmnopqrstuvwx" not in blob
