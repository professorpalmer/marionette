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


def test_vault_recalls_battery_catalog_miss_probe(tmp_path):
    from pmharness.compaction_residual_battery import RESIDUAL_CASES

    case = next(c for c in RESIDUAL_CASES if c.id == "catalog_miss_plain_fact")
    written = index_elided_messages(str(tmp_path), "sess-miss", list(case.transcript))
    assert written >= 1
    section = build_turn_vault_section(
        str(tmp_path),
        "sess-miss",
        case.probe_prompt,
    )
    lowered = section.lower()
    for token in case.must_contain:
        assert token.lower() in lowered


def test_vault_only_prose_selected_story_and_vault_hit(tmp_path):
    from harness.compaction_residual import build_catalog_residual
    from pmharness.compaction_residual_battery import cases_by_id

    case = cases_by_id()["vault_only_prose_cutoff"]
    catalog = build_catalog_residual(list(case.transcript), char_budget=4000)
    assert "fourteenth of each month" in catalog.lower()
    written = index_elided_messages(str(tmp_path), "sess-prose", list(case.transcript))
    assert written >= 1
    section = build_turn_vault_section(str(tmp_path), "sess-prose", case.probe_prompt)
    assert "fourteenth of each month" in section.lower()


def test_vault_narrative_and_paraphrase_miss_lexical_twin_false_hits(tmp_path):
    from harness.compaction_residual import build_catalog_residual
    from harness.compaction_vault import vault_match_query
    from pmharness.compaction_residual_battery import cases_by_id
    from pmharness.compaction_residual_live import score_end_task_text

    narrative = cases_by_id()["vault_narrative_no_overlap"]
    paraphrase = cases_by_id()["vault_paraphrase_no_overlap"]
    twin = cases_by_id()["vault_false_retrieve_twin"]
    for case, sid in (
        (narrative, "sess-narr"),
        (paraphrase, "sess-para"),
        (twin, "sess-twin"),
    ):
        catalog = build_catalog_residual(list(case.transcript), char_budget=4000)
        lowered = catalog.lower()
        for token in case.must_contain:
            assert token.lower() in lowered
        index_elided_messages(str(tmp_path), sid, list(case.transcript))

    recap_q = vault_match_query(narrative.probe_prompt)
    assert "spare" not in recap_q.lower()
    assert is_recap_ask(narrative.probe_prompt) is True
    raw_fts = vault_match_query(narrative.probe_prompt)
    assert "Remind" in raw_fts or "decided" in raw_fts
    plan = build_plan_recap_chunk(list(narrative.transcript))
    assert "spare region" in plan.lower()
    narr = retrieve_vault_result(str(tmp_path), "sess-narr", narrative.probe_prompt)
    assert narr["route"] == "recap_plan"
    assert "spare region" in "\n".join(narr["hits"]).lower()
    assert "earlier facts" not in "\n".join(narr["hits"]).lower()

    para_q = vault_match_query(paraphrase.probe_prompt)
    assert "twenty" not in para_q.lower()
    assert "omega" not in para_q.lower()
    para = retrieve_vault_result(str(tmp_path), "sess-para", paraphrase.probe_prompt)
    assert para["route"] == "empty"
    assert para["hits"] == []

    twin_hits = retrieve_vault_chunks(str(tmp_path), "sess-twin", twin.probe_prompt)
    twin_blob = "\n".join(twin_hits).lower()
    assert "spare region" in twin_blob
    assert "primary region" not in twin_blob
    twin_section = build_turn_vault_section(
        str(tmp_path), "sess-twin", twin.probe_prompt
    )
    assert len(twin_section) >= 80

    assert score_end_task_text(paraphrase, "Invoices freeze on the 27th.")[
        "end_task_success"
    ] is True
    assert score_end_task_text(paraphrase, "Invoices freeze mid-month.")[
        "end_task_success"
    ] is False


def test_vault_peek_evicted_case_drops_archive_and_keeps_vault(tmp_path):
    import json

    from harness.compaction_archive import retain_archive_messages
    from pmharness.compaction_residual_battery import cases_by_id

    case = cases_by_id()["vault_peek_evicted_cutoff"]
    assert case.hide_peek is False
    retained = retain_archive_messages(list(case.transcript))
    assert "fourteenth" not in json.dumps(retained).lower()
    written = index_elided_messages(str(tmp_path), "sess-evict-case", list(case.transcript))
    assert written >= 1
    section = build_turn_vault_section(
        str(tmp_path),
        "sess-evict-case",
        case.probe_prompt,
    )
    assert "fourteenth of each month" in section.lower()


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
    from pmharness.compaction_residual_battery import cases_by_id

    reversal = cases_by_id()["unprefixed_reversal"]
    plan = build_plan_recap_chunk(list(reversal.transcript))
    low = plan.lower()
    assert "write to the live ledger now" in low
    assert "east replica is retired" in low
    assert "don't write" not in low
    assert "only sink" not in low
    assert "reversed." not in low

    twin = cases_by_id()["vault_false_retrieve_twin"]
    twin_plan = build_plan_recap_chunk(list(twin.transcript))
    assert "spare region" in twin_plan.lower()
    assert "primary region" not in twin_plan.lower()

    kept = cases_by_id()["unprefixed_obligation"]
    kept_plan = build_plan_recap_chunk(list(kept.transcript))
    assert "don't write" in kept_plan.lower() or "live ledger" in kept_plan.lower()

    index_elided_messages(str(tmp_path), "sess-rev", list(reversal.transcript))
    rev_hit = retrieve_vault_result(
        str(tmp_path), "sess-rev", reversal.probe_prompt
    )
    rev_blob = "\n".join(rev_hit["hits"]).lower()
    assert "write to the live ledger now" in rev_blob
    assert "don't write" not in rev_blob
    assert "only sink" not in rev_blob

    index_elided_messages(str(tmp_path), "sess-keep", list(kept.transcript))
    keep_hit = retrieve_vault_result(
        str(tmp_path), "sess-keep", kept.probe_prompt
    )
    keep_blob = "\n".join(keep_hit["hits"]).lower()
    assert "live ledger" in keep_blob


def test_vault_selector_default_worthy_contract(tmp_path):
    """Last-N story, no miss_plan, tighter recap. These used to fail."""
    from harness.compaction_vault import _filler_like
    from pmharness.compaction_residual_battery import cases_by_id

    catalog = cases_by_id()
    leak = catalog["vault_selector_plausible_filler"]
    kept = catalog["vault_selector_docs_only_plan"]
    capped = catalog["vault_selector_cap_drops_late"]
    assistant = catalog["vault_selector_assistant_only"]
    wrong = catalog["vault_selector_miss_wrong_plan"]
    false_fire = catalog["vault_recap_false_fire"]

    leak_plan = build_plan_recap_chunk(list(leak.transcript))
    assert "spare region" in leak_plan.lower()
    assert "warmer" in leak_plan.lower()

    assert not _filler_like(
        "please keep this docs only: ship the canary to the spare "
        "region before Friday."
    )
    kept_plan = build_plan_recap_chunk(list(kept.transcript))
    assert "spare region" in kept_plan.lower()
    index_elided_messages(str(tmp_path), "sess-docs", list(kept.transcript))
    kept_hit = retrieve_vault_result(
        str(tmp_path), "sess-docs", kept.probe_prompt
    )
    assert kept_hit["route"] == "recap_plan"
    assert "spare region" in "\n".join(kept_hit["hits"]).lower()

    capped_plan = build_plan_recap_chunk(list(capped.transcript))
    assert "spare region" in capped_plan.lower()
    assert "primary region" not in capped_plan.lower()

    assistant_plan = build_plan_recap_chunk(list(assistant.transcript))
    assert "spare region" in assistant_plan.lower()

    index_elided_messages(str(tmp_path), "sess-wrong", list(wrong.transcript))
    wrong_hit = retrieve_vault_result(str(tmp_path), "sess-wrong", wrong.probe_prompt)
    assert wrong_hit["route"] == "empty"
    assert wrong_hit["hits"] == []

    assert is_recap_ask(false_fire.probe_prompt) is False
    index_elided_messages(str(tmp_path), "sess-fire", list(false_fire.transcript))
    fire_hit = retrieve_vault_result(
        str(tmp_path), "sess-fire", false_fire.probe_prompt
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
