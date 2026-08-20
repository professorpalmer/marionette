"""Characterization tests for the economics API peel."""
from __future__ import annotations

from types import SimpleNamespace

from harness.api.economics import EconomicsServices, get_economics


def _svc(*, repo="/workspace", jobs=None, session_id="sess-1"):
    jobs = list(jobs or [])

    return EconomicsServices(
        cfg=SimpleNamespace(repo=repo),
        scoped_jobs_with_stores=lambda repo_root=None: (list(jobs), _Store(), _Store()),
        diag=lambda *a, **k: None,
        active_session_id=lambda: session_id,
    )


class _Store:
    def list_artifacts(self, job_id):
        return []

    def get_job(self, job_id):
        return SimpleNamespace(status="complete", created_at="", completed_at=None)

    def list_tasks(self, job_id):
        return []


class _JobCost:
    def __init__(self, usd=1.25, avoided=2.5, model="composer-2"):
        self.total_marginal_cost_usd = usd
        self.measured_cost_usd = usd
        self.estimated_cost_usd = 0.0
        self.by_model = {
            model: {
                "calls": 1,
                "tokens_in": 100,
                "tokens_out": 50,
                "marginal_cost_usd": usd,
                "billing": "metered",
            }
        }
        self.tasks = []


def _report(*, window_days=None, reference_model_id="anthropic/claude-opus-4"):
    return {
        "window_days": window_days,
        "jobs_considered": 1,
        "routing": SimpleNamespace(
            saved_usd=1.5,
            baseline_usd=3.0,
            chosen_usd=1.5,
            pct_cheaper=50.0,
            plan_routed_tasks=1,
            tasks_total=2,
            tasks_without_baseline=0,
            cost_optimizing_tasks=1,
            deliberate_spend_usd=0.0,
            deliberate_tasks=0,
        ),
        "self_heal": SimpleNamespace(fallbacks=0, escalations=0),
        "codegraph": {"dollars_saved_est": 0.4, "queries": 2, "context_tokens_fed": 1000},
        "reads": {},
        "memory_cost": {},
        "tool_offload": {"offloads": 0, "tokens_saved": 0, "chars_saved": 0},
        "metrics": {},
        "counterfactual": SimpleNamespace(
            reference_model_id=reference_model_id,
            reference_priced=True,
            naive_cost_usd=9.0,
            actual_cost_usd=2.0,
            avoided_usd=7.0,
            tasks=2,
        ),
    }


def _patch_pm(monkeypatch, *, build_report=None, extra_dirs=None, opened=None):
    opened = opened if opened is not None else []
    reports = []

    def fake_build_report(stores, window_days=None):
        reports.append({"stores": list(stores), "window_days": window_days})
        if build_report is not None:
            return build_report(stores, window_days=window_days)
        return _report(window_days=window_days)

    monkeypatch.setattr("harness.cli_job_merge.resolve_cli_state_dir", lambda ws: "/tmp/pm-primary")
    monkeypatch.setattr(
        "harness.cli_job_merge.is_marionette_host_scratch_dir",
        lambda path: False,
    )
    monkeypatch.setattr(
        "puppetmaster.store_factory.create_store",
        lambda backend, path: opened.append((backend, str(path))) or object(),
    )
    monkeypatch.setattr("puppetmaster.savings.build_report", fake_build_report)
    monkeypatch.setattr(
        "puppetmaster.state.list_project_state_dirs",
        lambda: list(extra_dirs or []),
    )
    monkeypatch.setattr("puppetmaster.model_registry.load_registry", lambda: [])
    monkeypatch.setattr("puppetmaster.cost.price_job", lambda arts, reg: _JobCost())
    monkeypatch.setattr(
        "puppetmaster.cost.job_counterfactual",
        lambda job_cost, reg: SimpleNamespace(
            reference_model_id="anthropic/claude-opus-4",
            reference_priced=True,
            naive_cost_usd=4.0,
            actual_cost_usd=1.25,
            avoided_usd=2.75,
            tasks=1,
        ),
    )
    monkeypatch.setattr(
        "puppetmaster.receipt.build_job_receipt",
        lambda store, job_id: {
            "tokens": {"total_tokens": 800},
            "artifacts": {"typed_total": 2},
            "efficiency": {"tokens_per_typed_artifact": 400.0, "degraded_rate": 0.0},
            "tasks": {"degraded": 0},
        },
    )
    return reports, opened


def test_get_economics_default_scope_repo(monkeypatch):
    reports, opened = _patch_pm(monkeypatch)
    code, payload = get_economics({}, _svc())
    assert code == 200
    assert payload["available"] is True
    assert payload["scope"] == "repo"
    assert payload["all_projects"] is False
    assert payload["window_days"] is None
    assert reports == [{"stores": reports[0]["stores"], "window_days": None}]
    assert reports[0]["window_days"] is None
    assert len(opened) == 1
    assert opened[0] == ("sqlite", "/tmp/pm-primary")


def test_get_economics_window30_passes_window_days(monkeypatch):
    reports, _opened = _patch_pm(monkeypatch)
    code, payload = get_economics({"scope": ["window30"]}, _svc())
    assert code == 200
    assert payload["scope"] == "window30"
    assert payload["window_days"] == 30.0
    assert reports[0]["window_days"] == 30.0
    assert payload["all_projects"] is False


def test_get_economics_all_projects_opens_extra_dirs(tmp_path, monkeypatch):
    extra = tmp_path / "other-project"
    extra.mkdir()
    opened = []
    reports, opened = _patch_pm(monkeypatch, extra_dirs=[extra], opened=opened)
    code, payload = get_economics({"scope": ["all_projects"]}, _svc())
    assert code == 200
    assert payload["all_projects"] is True
    assert payload["window_days"] is None
    assert reports[0]["window_days"] is None
    opened_paths = [path for _backend, path in opened]
    assert "/tmp/pm-primary" in opened_paths
    assert str(extra) in opened_paths
    assert len(opened_paths) == 2


def test_visibility_only_job_listed_but_omitted_from_owned_totals(monkeypatch):
    jobs = [
        {
            "id": "owned-1",
            "status": "complete",
            "source": "harness",
            "accounting_owned": True,
            "accounting_scope": "marionette",
            "created_at": "2026-08-20T00:00:00+00:00",
        },
        {
            "id": "vis-1",
            "status": "complete",
            "source": "cli",
            "accounting_owned": False,
            "accounting_scope": "visibility_only",
            "created_at": "2026-08-20T00:01:00+00:00",
        },
    ]
    costs = {
        "owned-1": _JobCost(usd=1.25, model="owned-model"),
        "vis-1": _JobCost(usd=9.99, model="foreign-model"),
    }

    class _IdStore(_Store):
        def __init__(self):
            self.last = None

        def list_artifacts(self, job_id):
            self.last = job_id
            return [{"_job_id": job_id}]

    id_store = _IdStore()

    def price_by_art(arts, reg):
        jid = None
        if arts and isinstance(arts[0], dict):
            jid = arts[0].get("_job_id")
        return costs.get(jid, _JobCost(usd=1.25))

    def cf_by_cost(job_cost, reg):
        usd = float(getattr(job_cost, "total_marginal_cost_usd", 0) or 0)
        return SimpleNamespace(
            reference_model_id="anthropic/claude-opus-4",
            reference_priced=True,
            naive_cost_usd=usd + 2.0,
            actual_cost_usd=usd,
            avoided_usd=2.0 if usd < 5 else 8.0,
            tasks=1,
        )

    _patch_pm(monkeypatch)
    monkeypatch.setattr("puppetmaster.cost.price_job", price_by_art)
    monkeypatch.setattr("puppetmaster.cost.job_counterfactual", cf_by_cost)

    svc = EconomicsServices(
        cfg=SimpleNamespace(repo="/workspace"),
        scoped_jobs_with_stores=lambda repo_root=None: (list(jobs), id_store, id_store),
        diag=lambda *a, **k: None,
        active_session_id=lambda: "sess-1",
    )
    code, payload = get_economics({"scope": ["repo"]}, svc)
    assert code == 200
    rows = {row["job_id"]: row for row in payload["recent_jobs"]}
    assert "vis-1" in rows
    assert rows["vis-1"]["accounting_owned"] is False
    assert rows["vis-1"]["actual_marginal_usd"] is None
    assert rows["vis-1"]["measured_cost_usd"] is None
    assert rows["owned-1"]["actual_marginal_usd"] == 1.25
    assert payload["owned_jobs_considered"] == 1
    assert payload["owned_actual_marginal_usd"] == 1.25
    assert payload["owned_avoided_usd"] == 2.0
    assert payload["owned_actual_marginal_usd"] != 1.25 + 9.99


def test_reference_model_id_passed_through(monkeypatch):
    kept = "kept/reference-model-id"
    _patch_pm(
        monkeypatch,
        build_report=lambda stores, window_days=None: _report(
            window_days=window_days, reference_model_id=kept
        ),
    )
    code, payload = get_economics({}, _svc())
    assert code == 200
    assert payload["counterfactual"]["reference_model_id"] == kept
    assert payload["savings"]["counterfactual"]["reference_model_id"] == kept


def test_pm_failure_returns_available_false(monkeypatch):
    _patch_pm(monkeypatch)
    monkeypatch.setattr(
        "puppetmaster.savings.build_report",
        lambda stores, window_days=None: (_ for _ in ()).throw(
            RuntimeError("store is locked")
        ),
    )
    code, payload = get_economics({}, _svc())
    assert code == 200
    assert payload["available"] is False
    assert "locked" in payload["error"]
    assert payload["savings"] is None
    assert payload["recent_jobs"] == []


def test_conversation_scope_filters_to_owned_active_session(monkeypatch):
    jobs = [
        {
            "id": "here",
            "status": "complete",
            "source": "harness",
            "accounting_owned": True,
            "accounting_scope": "marionette",
            "session_id": "sess-1",
            "created_at": "2026-08-20T00:02:00+00:00",
        },
        {
            "id": "other-session",
            "status": "complete",
            "source": "harness",
            "accounting_owned": True,
            "accounting_scope": "marionette",
            "session_id": "sess-2",
            "created_at": "2026-08-20T00:03:00+00:00",
        },
        {
            "id": "vis-1",
            "status": "complete",
            "source": "cli",
            "accounting_owned": False,
            "accounting_scope": "visibility_only",
            "session_id": "sess-1",
            "created_at": "2026-08-20T00:04:00+00:00",
        },
    ]
    reports, _opened = _patch_pm(monkeypatch)
    code, payload = get_economics({"scope": ["conversation"]}, _svc(jobs=jobs))
    assert code == 200
    assert payload["scope"] == "conversation"
    assert payload["savings_scope"] == "repo"
    assert payload["all_projects"] is False
    assert reports[0]["window_days"] is None
    assert [row["job_id"] for row in payload["recent_jobs"]] == ["here"]
    assert payload["owned_jobs_considered"] == 1


def test_conversation_excludes_owned_jobs_without_session(monkeypatch):
    jobs = [
        {
            "id": "unstamped",
            "status": "complete",
            "source": "harness",
            "accounting_owned": True,
            "accounting_scope": "marionette",
            "created_at": "2026-08-20T00:05:00+00:00",
        },
    ]
    _patch_pm(monkeypatch)
    code, payload = get_economics({"scope": ["conversation"]}, _svc(jobs=jobs))
    assert code == 200
    assert payload["recent_jobs"] == []
    assert payload["owned_jobs_considered"] == 0


def test_invalid_scope_returns_400():
    code, payload = get_economics({"scope": ["nope"]}, _svc())
    assert code == 400
    assert "error" in payload
