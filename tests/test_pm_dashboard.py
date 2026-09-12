"""Stock Puppetmaster dashboard host — reuse runfile, never a second runtime."""

from harness.pm_dashboard import (
    build_dashboard_url,
    ensure_local_dashboard,
    is_dashboard_job_id,
    resolve_dashboard_state_dir,
    try_warm_local_dashboard,
)
from harness.api.dashboard import get_dashboard
from harness.api.jobs import make_job_services


def test_build_dashboard_url_keeps_embed_and_job():
    assert build_dashboard_url("127.0.0.1", 8787, "job_abcdef012345") == (
        "http://127.0.0.1:8787/?job=job_abcdef012345&embed=1"
    )
    assert build_dashboard_url("127.0.0.1", 8791) == "http://127.0.0.1:8791/?embed=1"
    assert is_dashboard_job_id("job_abcdef012345")
    assert not is_dashboard_job_id("../etc/passwd")
    assert not is_dashboard_job_id("")
    assert not is_dashboard_job_id("local-swarm-call_1799376")
    assert not is_dashboard_job_id("job_")


def test_get_dashboard_rejects_unsafe_job_id():
    status, payload = get_dashboard({"job": ["../secret"]}, make_job_services())
    assert status == 400
    assert payload["ok"] is False


def test_get_dashboard_strips_benign_local_alias(monkeypatch):
    monkeypatch.setattr(
        "harness.api.dashboard.resolve_dashboard_state_dir",
        lambda repo, job_id: "/tmp/pm-state",
    )
    seen = {}

    def _ensure(**kwargs):
        seen.update(kwargs)
        return {
            "ok": True,
            "reused": True,
            "host": "127.0.0.1",
            "port": 8788,
            "url": "http://127.0.0.1:8788/?embed=1",
        }

    monkeypatch.setattr("harness.api.dashboard.ensure_local_dashboard", _ensure)
    svc = make_job_services(cfg=type("Cfg", (), {"repo": "/work/repo"})())
    status, payload = get_dashboard({"job": ["local-swarm-call_1799376"]}, svc)
    assert status == 200
    assert payload["ok"] is True
    assert seen.get("job_id") in (None, "")
    assert "job=" not in payload["embed_url"]


def test_get_dashboard_reuses_tracked_runtime(monkeypatch):
    monkeypatch.setattr(
        "harness.api.dashboard.resolve_dashboard_state_dir",
        lambda repo, job_id: "/tmp/pm-state",
    )
    monkeypatch.setattr(
        "harness.api.dashboard.ensure_local_dashboard",
        lambda **_k: {
            "ok": True,
            "reused": True,
            "host": "127.0.0.1",
            "port": 8788,
            "url": "http://127.0.0.1:8788/?job=job_abcdef012345&embed=1",
        },
    )
    svc = make_job_services(cfg=type("Cfg", (), {"repo": "/work/repo"})())
    status, payload = get_dashboard({"job": ["job_abcdef012345"]}, svc)
    assert status == 200
    assert payload["reused"] is True
    assert payload["embed_url"].endswith("embed=1")
    assert "job=job_abcdef012345" in payload["url"]


def test_get_dashboard_reports_missing_state_dir(monkeypatch):
    monkeypatch.setattr(
        "harness.api.dashboard.resolve_dashboard_state_dir",
        lambda repo, job_id: None,
    )
    status, payload = get_dashboard({"job": ["job_abcdef012345"]}, make_job_services())
    assert status == 503
    assert payload["error"] == "state_dir_unavailable"


def test_get_dashboard_heals_missing_store(monkeypatch):
    monkeypatch.setattr(
        "harness.api.dashboard.resolve_dashboard_state_dir",
        lambda repo, job_id: None,
    )
    monkeypatch.setattr(
        "harness.cli_job_merge.ensure_workspace_project_store",
        lambda repo: "/tmp/healed-pm-state" if repo == "/work/new-kit" else None,
    )
    monkeypatch.setattr(
        "harness.api.dashboard.ensure_local_dashboard",
        lambda **_k: {
            "ok": True,
            "reused": False,
            "host": "127.0.0.1",
            "port": 8788,
            "url": "http://127.0.0.1:8788/?embed=1",
        },
    )
    svc = make_job_services(cfg=type("Cfg", (), {"repo": "/work/new-kit"})())
    status, payload = get_dashboard({}, svc)
    assert status == 200
    assert payload["ok"] is True


def test_ensure_local_dashboard_reuses_runfile(monkeypatch):
    calls = []

    monkeypatch.setattr(
        "harness.pm_dashboard._reuse_tracked_dashboard",
        lambda *_a, **_k: {
            "ok": True,
            "reused": True,
            "host": "127.0.0.1",
            "port": 8787,
            "pid": 11,
            "state_dir": "/tmp/pm-state",
        },
    )

    def boom(*_a, **_k):
        calls.append("spawn")
        raise AssertionError("must not spawn a second dashboard")

    monkeypatch.setattr("harness.pm_dashboard._spawn_dashboard_cli", boom)
    out = ensure_local_dashboard(state_dir="/tmp/pm-state", job_id="job_abcdef012345")
    assert out["reused"] is True
    assert out["url"] == "http://127.0.0.1:8787/?job=job_abcdef012345&embed=1"
    assert calls == []


def test_resolve_dashboard_state_dir_falls_back_to_cli_dir(monkeypatch):
    monkeypatch.setattr(
        "harness.cli_job_merge.resolve_cli_state_dir",
        lambda repo: "/tmp/from-repo" if repo == "/work" else None,
    )
    assert resolve_dashboard_state_dir("/work", "") == "/tmp/from-repo"


def test_try_warm_local_dashboard_skips_empty_state_dir():
    assert try_warm_local_dashboard("") == {
        "ok": False,
        "error": "state_dir_unavailable",
    }
    assert try_warm_local_dashboard("   ")["error"] == "state_dir_unavailable"


def test_try_warm_local_dashboard_reuses_ensure(monkeypatch):
    seen = {}

    def _ensure(**kwargs):
        seen.update(kwargs)
        return {"ok": True, "reused": True, "state_dir": kwargs["state_dir"]}

    monkeypatch.setattr("harness.pm_dashboard.ensure_local_dashboard", _ensure)
    out = try_warm_local_dashboard("/tmp/pm-state")
    assert out["ok"] is True
    assert seen["state_dir"] == "/tmp/pm-state"
