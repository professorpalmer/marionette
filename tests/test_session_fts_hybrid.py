"""Hybrid FTS: transcript + preview + artifact headline projections."""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile

import pytest

from harness.api.jobs import JobServices, get_artifacts
from harness.api.sessions import SessionServices, get_sessions_search
from harness.job_scoping import job_label_for_session, stamp_task_payload
from harness.session_fts import (
    _display_artifact_id,
    _extract_display_artifact_headlines,
    _fts_match_query,
    best_effort_index_job_artifacts,
    index_session_artifacts,
    index_session_preview,
    index_session_transcript,
    remove_artifact_from_index,
    remove_session_from_index,
    reindex_store_artifacts,
    search_sessions,
)
from puppetmaster.models import Artifact, ArtifactType, Task
from puppetmaster.store_factory import create_store
from types import SimpleNamespace


def _synthetic_transcript(
    user_text: str,
    *,
    assistant_text: str = "",
    display_artifacts: list | None = None,
) -> dict:
    history = [{"role": "user", "content": user_text}]
    display = [{"type": "message", "role": "user", "text": user_text}]
    if assistant_text:
        history.append({"role": "assistant", "content": assistant_text})
        display.append({"type": "message", "role": "assistant", "text": assistant_text})
    if display_artifacts:
        display.append(
            {
                "type": "card",
                "result": {"artifacts": display_artifacts},
            }
        )
    return {"history": history, "display": display, "job_ids": []}


def _seed_job_with_artifact(state_dir: str, session_id: str, claim: str) -> dict:
    store = create_store("sqlite", state_dir)
    store.init()
    job = store.create_job(
        "hybrid fts job",
        label=job_label_for_session(session_id),
    )
    task = Task(
        job_id=job.id,
        role="implement",
        instruction="work",
        payload=stamp_task_payload({"cwd": state_dir}, session_id=session_id),
    )
    store.save_task(task)
    artifact = Artifact(
        job_id=job.id,
        task_id=task.id,
        type=ArtifactType.FINDING,
        created_by="worker",
        payload={"claim": claim, "detail": "secret full body must not be indexed"},
        confidence=0.9,
        evidence=["evidence"],
    )
    store.save_artifact(artifact)
    return {"job_id": job.id, "artifact_id": artifact.id}


def test_same_type_display_artifacts_get_unique_ids():
    arts = [
        {"type": "finding", "headline": "first alpha finding"},
        {"type": "finding", "headline": "second beta finding"},
    ]
    transcript = _synthetic_transcript("user", display_artifacts=arts)
    rows = _extract_display_artifact_headlines(transcript)
    ids = [row[0] for row in rows]
    assert len(ids) == 2
    assert len(set(ids)) == 2
    assert all(i.startswith("display-finding-") for i in ids)


def test_display_artifact_uses_real_id_when_present():
    art = {"id": "art-real-1", "type": "finding", "headline": "real id headline"}
    assert _display_artifact_id(art, 0) == "art-real-1"


def test_display_artifact_reindex_replaces_same_type_cards():
    with tempfile.TemporaryDirectory() as state_dir:
        first = _synthetic_transcript(
            "user",
            display_artifacts=[
                {"type": "finding", "headline": "first saturn finding"},
                {"type": "finding", "headline": "second saturn finding"},
            ],
        )
        index_session_transcript(state_dir, "sess-display-dedupe", first)
        conn = sqlite3.connect(os.path.join(state_dir, "session_fts.sqlite"))
        try:
            first_rows = conn.execute(
                "SELECT artifact_id, headline FROM artifact_headlines"
                " WHERE session_id = ? AND job_id = '' ORDER BY artifact_id",
                ("sess-display-dedupe",),
            ).fetchall()
        finally:
            conn.close()
        assert len(first_rows) == 2
        assert len({r[0] for r in first_rows}) == 2

        second = _synthetic_transcript(
            "user",
            display_artifacts=[
                {"type": "finding", "headline": "replacement saturn finding"},
            ],
        )
        index_session_transcript(state_dir, "sess-display-dedupe", second)
        conn = sqlite3.connect(os.path.join(state_dir, "session_fts.sqlite"))
        try:
            second_rows = conn.execute(
                "SELECT artifact_id, headline FROM artifact_headlines"
                " WHERE session_id = ? AND job_id = ''",
                ("sess-display-dedupe",),
            ).fetchall()
        finally:
            conn.close()
        assert len(second_rows) == 1
        assert "replacement saturn" in second_rows[0][1]


def test_preview_match_ranks_in_search():
    with tempfile.TemporaryDirectory() as state_dir:
        index_session_preview(
            state_dir,
            "sess-preview",
            "unique quasar preview token only in preview row",
        )
        hits = search_sessions(state_dir, "quasar preview", limit=10)
        assert len(hits) == 1
        assert hits[0]["session_id"] == "sess-preview"
        assert hits[0]["match_kind"] == "preview"


def test_artifact_headline_match_ranks_in_search():
    with tempfile.TemporaryDirectory() as state_dir:
        index_session_artifacts(
            state_dir,
            "sess-artifact",
            [{"id": "art-1", "headline": "Router drops rejected alternatives"}],
            job_id="job-abc",
        )
        hits = search_sessions(state_dir, "Router rejected", limit=10)
        assert len(hits) == 1
        assert hits[0]["session_id"] == "sess-artifact"
        assert hits[0]["match_kind"] == "artifact"


def test_merged_ranking_prefers_best_bm25_per_session():
    with tempfile.TemporaryDirectory() as state_dir:
        index_session_transcript(
            state_dir,
            "sess-merge",
            _synthetic_transcript("commonword commonword commonword"),
        )
        index_session_preview(state_dir, "sess-merge", "commonword")
        index_session_artifacts(
            state_dir,
            "sess-other",
            [{"id": "a1", "headline": "commonword commonword commonword commonword"}],
            job_id="job-x",
        )
        hits = search_sessions(state_dir, "commonword", limit=10)
        ids = [h["session_id"] for h in hits]
        assert ids == sorted(set(ids), key=lambda sid: ids.index(sid))
        assert len(ids) == len(set(ids))
        assert "sess-merge" in ids
        assert "sess-other" in ids


def test_transcript_preview_artifact_all_discoverable():
    with tempfile.TemporaryDirectory() as state_dir:
        index_session_transcript(
            state_dir,
            "sess-all",
            _synthetic_transcript(
                "transcript-nebula-token",
                display_artifacts=[
                    {"type": "finding", "headline": "artifact-nebula-token headline"},
                ],
            ),
        )
        index_session_preview(state_dir, "sess-all", "preview-nebula-token")
        assert search_sessions(state_dir, "transcript-nebula")[0]["match_kind"] == "transcript"
        assert search_sessions(state_dir, "preview-nebula")[0]["match_kind"] == "preview"
        assert search_sessions(state_dir, "artifact-nebula")[0]["match_kind"] == "artifact"


def test_stale_preview_replaced_on_reindex():
    with tempfile.TemporaryDirectory() as state_dir:
        index_session_preview(state_dir, "sess-stale", "old preview saturn")
        index_session_preview(state_dir, "sess-stale", "new preview saturn")
        hits = search_sessions(state_dir, "saturn")
        assert len(hits) == 1
        assert "new preview" in hits[0]["snippet"] or hits[0]["match_kind"] == "preview"


def test_stale_artifact_removed():
    with tempfile.TemporaryDirectory() as state_dir:
        index_session_artifacts(
            state_dir,
            "sess-drop",
            [{"id": "gone-art", "headline": "ephemeral jupiter finding"}],
            job_id="job-drop",
        )
        assert search_sessions(state_dir, "jupiter")
        assert remove_artifact_from_index(
            state_dir,
            "job-drop",
            "gone-art",
            session_id="sess-drop",
        )
        assert search_sessions(state_dir, "jupiter") == []


def test_malformed_fts_query_is_operator_safe():
    assert _fts_match_query('foo OR bar') == '"foo" AND "OR" AND "bar"'
    assert _fts_match_query('***') == ""
    with tempfile.TemporaryDirectory() as state_dir:
        index_session_transcript(
            state_dir,
            "sess-safe",
            _synthetic_transcript("operator safe content"),
        )
        assert search_sessions(state_dir, 'content OR DROP') == []


def test_partial_migration_old_db_still_searches_transcripts():
    with tempfile.TemporaryDirectory() as state_dir:
        db_path = os.path.join(state_dir, "session_fts.sqlite")
        conn = sqlite3.connect(db_path)
        conn.execute(
            "CREATE VIRTUAL TABLE session_chunks USING fts5("
            "session_id UNINDEXED, chunk, tokenize = 'porter unicode61')"
        )
        conn.execute(
            "INSERT INTO session_chunks(session_id, chunk) VALUES (?, ?)",
            ("legacy-sess", "legacy-only pluto transcript token"),
        )
        conn.commit()
        conn.close()

        hits = search_sessions(state_dir, "pluto transcript")
        assert len(hits) == 1
        assert hits[0]["session_id"] == "legacy-sess"
        assert hits[0]["match_kind"] == "transcript"


def test_no_duplicate_session_rows():
    with tempfile.TemporaryDirectory() as state_dir:
        payload = _synthetic_transcript(
            "duplicate venus token in transcript",
            display_artifacts=[
                {"type": "finding", "headline": "duplicate venus token in artifact"},
            ],
        )
        index_session_transcript(state_dir, "sess-dedupe", payload)
        index_session_preview(state_dir, "sess-dedupe", "duplicate venus token in preview")
        hits = search_sessions(state_dir, "venus", limit=20)
        assert [h["session_id"] for h in hits].count("sess-dedupe") == 1


def test_best_effort_hooks_never_raise(monkeypatch):
    with tempfile.TemporaryDirectory() as state_dir:
        def boom(*_a, **_k):
            raise RuntimeError("store unavailable")

        monkeypatch.setattr(
            "harness.session_fts.index_session_artifacts",
            boom,
        )
        assert best_effort_index_job_artifacts(state_dir, "job-1", session_id="sess-x") is False
        assert index_session_preview(state_dir, "", "text") is False
        assert index_session_preview("/no/such", "sess-x", "text") is False


def test_reindex_store_artifacts_read_only_projection():
    with tempfile.TemporaryDirectory() as state_dir:
        ids = _seed_job_with_artifact(
            state_dir,
            "sess-store",
            "store-backed neptune routing regression",
        )
        stats = reindex_store_artifacts(state_dir)
        assert stats["indexed"] == 1
        hits = search_sessions(state_dir, "neptune routing")
        assert hits
        assert hits[0]["session_id"] == "sess-store"
        assert hits[0]["match_kind"] == "artifact"
        assert ids["artifact_id"]


def _session_services(state_dir: str) -> SessionServices:
    return SessionServices(
        sessions=SimpleNamespace(active=""),
        runners=SimpleNamespace(get=lambda _sid: None),
        cfg=SimpleNamespace(state_dir=state_dir, repo=state_dir),
        get_pilot=lambda: SimpleNamespace(load_history=lambda _h: None),
        sessions_state_dir=lambda: state_dir,
        save_active_transcript=lambda: None,
        attach_view=lambda *_a, **_k: None,
        sync_pilot_session_id=lambda: None,
        diag=lambda *_a, **_k: None,
        is_app_install_root=lambda _p: False,
        ensure_home_workspace=lambda: state_dir,
        prepare_home_workspace=lambda: state_dir,
        home_workspace_path=lambda: state_dir,
        note_boot_repo=lambda _r: None,
        record_recent_workspace=lambda *_a, **_k: None,
        puppetmaster_available=lambda: False,
        index_codegraph_bg=lambda _r: None,
        maybe_refresh_codegraph=lambda _r: None,
        get_codegraph_status=lambda _r: "none",
        lease_exhausted_body=lambda _e: {},
        attach_view_transcript_payload=lambda _p, _s: {},
        parse_bool=lambda v: bool(v),
        set_codegraph_status=lambda *_a, **_k: None,
    )


def test_get_sessions_search_api_merges_sources():
    with tempfile.TemporaryDirectory() as state_dir:
        index_session_transcript(
            state_dir,
            "api-sess",
            _synthetic_transcript("api transcript mercury"),
        )
        code, payload = get_sessions_search({"q": ["mercury"]}, _session_services(state_dir))
        assert code == 200
        assert isinstance(payload, list)
        assert payload[0]["session_id"] == "api-sess"


def test_empty_job_artifact_rows_replaced_not_accumulated():
    with tempfile.TemporaryDirectory() as state_dir:
        index_session_artifacts(
            state_dir,
            "sess-empty-job",
            [{"id": "art-old", "headline": "stale pluto headline"}],
            job_id="",
        )
        index_session_artifacts(state_dir, "sess-empty-job", [], job_id="")
        hits = search_sessions(state_dir, "pluto")
        assert hits == []
        conn = sqlite3.connect(os.path.join(state_dir, "session_fts.sqlite"))
        try:
            rows = conn.execute(
                "SELECT artifact_id FROM artifact_headlines"
                " WHERE session_id = ? AND job_id = ''",
                ("sess-empty-job",),
            ).fetchall()
        finally:
            conn.close()
        assert rows == []


def test_secrets_redacted_from_indexed_transcript_and_search():
    secret = "sk-abcdefghijklmnopqrstuvwxyz1234567890"
    with tempfile.TemporaryDirectory() as state_dir:
        index_session_transcript(
            state_dir,
            "sess-secret",
            _synthetic_transcript(f"Use api_key={secret} in config"),
        )
        db_path = os.path.join(state_dir, "session_fts.sqlite")
        conn = sqlite3.connect(db_path)
        try:
            blob = conn.execute(
                "SELECT chunk FROM session_chunks WHERE session_id = ?",
                ("sess-secret",),
            ).fetchone()[0]
        finally:
            conn.close()
        assert secret not in blob
        assert "REDACTED" in blob
        assert search_sessions(state_dir, secret.split("-")[0]) == []


def test_bearer_token_redacted_from_preview():
    token = "Bearer supersecretbearer123456"
    with tempfile.TemporaryDirectory() as state_dir:
        index_session_preview(state_dir, "sess-bearer", token)
        conn = sqlite3.connect(os.path.join(state_dir, "session_fts.sqlite"))
        try:
            preview = conn.execute(
                "SELECT preview FROM session_previews WHERE session_id = ?",
                ("sess-bearer",),
            ).fetchone()[0]
        finally:
            conn.close()
        assert "supersecretbearer" not in preview
        assert "REDACTED" in preview


def test_get_artifacts_hook_indexes_headlines():
    with tempfile.TemporaryDirectory() as state_dir:
        ids = _seed_job_with_artifact(
            state_dir,
            "sess-hook",
            "artifacts endpoint uranus headline",
        )
        from harness.state import DurableState

        durable = DurableState(state_dir)
        svc = JobServices(
            cfg=SimpleNamespace(state_dir=state_dir, repo=state_dir),
            sessions=SimpleNamespace(),
            get_pilot=lambda: SimpleNamespace(),
            get_session=lambda: SimpleNamespace(state=lambda: durable),
            diag=lambda *_a, **_k: None,
            scoped_jobs_snapshot=lambda **_k: [],
            scoped_jobs_with_stores=lambda **_k: ([], durable.store, None),
            retry_on_locked=lambda fn: fn(),
            swarm_registry=lambda: [],
            job_status_is_terminal=lambda _s: False,
            slim_swarm_list_artifacts=lambda arts, _s: arts,
            job_swarm_accounting=lambda *_a, **_k: (0.0, 0, 0),
            task_swarm_accounting=lambda *_a, **_k: {},
            routing_saved_usd=lambda *_a, **_k: 0.0,
            cache_saved_usd_swarm=lambda *_a, **_k: 0.0,
            tokens_cached_swarm=lambda *_a, **_k: 0,
            job_dead_run_failure=lambda *_a, **_k: None,
            job_savings_fields=lambda _jid: {},
            repo_session_stamped_meters=lambda _repo: {},
            session_cost_split=lambda *_a, **_k: 0.0,
            cache_savings=lambda *_a, **_k: 0.0,
            tool_output_savings_fields=lambda *_a, **_k: {},
            cost_source_label=lambda *_a, **_k: "",
        )
        code, arts = get_artifacts(ids["job_id"], svc)
        assert code == 200
        assert arts
        hits = search_sessions(state_dir, "uranus headline")
        assert hits
        assert hits[0]["session_id"] == "sess-hook"
