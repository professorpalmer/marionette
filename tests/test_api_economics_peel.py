"""Characterization tests for the economics API peel."""
from __future__ import annotations

from types import SimpleNamespace

from harness.api.economics import (
    EconomicsServices,
    _PrefetchedArtifacts,
    _aggregate_job_counterfactual,
    get_economics,
)


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
    def __init__(
        self,
        usd=1.25,
        avoided=2.5,
        model="composer-2",
        *,
        priced_tasks=1,
        unpriced_tasks=0,
        measured_runs=1,
        estimated_runs=0,
        estimated_usd=0.0,
        billing="metered",
    ):
        self.total_marginal_cost_usd = usd
        self.measured_cost_usd = usd
        self.estimated_cost_usd = estimated_usd
        self.priced_tasks = priced_tasks
        self.unpriced_tasks = unpriced_tasks
        self.measured_runs = measured_runs
        self.estimated_runs = estimated_runs
        self.by_model = {
            model: {
                "calls": 1,
                "tokens_in": 100,
                "tokens_out": 50,
                "marginal_cost_usd": usd,
                "billing": billing,
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

    def fake_cost_report(store, job_id, registry=None):
        from puppetmaster.cost import job_counterfactual, price_job

        job_cost = price_job(store.list_artifacts(job_id), registry or [])
        counterfactual = job_counterfactual(job_cost, registry or [])
        return {
            "job_id": job_id,
            "total_estimated_cost_usd": 0.0,
            "actual_cost": {
                "cost_basis": "measured_usage_x_registry_price",
                "total_marginal_cost_usd": job_cost.total_marginal_cost_usd,
                "measured_cost_usd": job_cost.measured_cost_usd,
                "estimated_cost_usd": job_cost.estimated_cost_usd,
                "measured_runs": job_cost.measured_runs,
                "estimated_runs": job_cost.estimated_runs,
                "priced_tasks": job_cost.priced_tasks,
                "unpriced_tasks": job_cost.unpriced_tasks,
                "by_model": job_cost.by_model,
                "tasks": [],
            },
            "counterfactual": vars(counterfactual) if counterfactual is not None else None,
            "tasks": [],
        }

    monkeypatch.setattr("puppetmaster.cost.build_cost_report", fake_cost_report)
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
    assert isinstance(payload, dict)
    assert payload["available"] is True
    assert payload["scope"] == "repo"
    assert payload["repo"] == "/workspace"
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


def test_recent_job_keeps_model_from_pm_financial_report(monkeypatch):
    _patch_pm(monkeypatch)
    monkeypatch.setattr(
        "harness.financial_receipt.load_pm_cost_report",
        lambda store, job_id, registry=None: {
            "job_id": job_id,
            "actual_cost": {
                "total_marginal_cost_usd": 1.25,
                "measured_cost_usd": 1.25,
                "estimated_cost_usd": 0.0,
                "measured_runs": 1,
                "estimated_runs": 0,
                "priced_tasks": 1,
                "unpriced_tasks": 0,
                "by_model": {
                    "composer-2": {
                        "billing": "metered", "calls": 1,
                        "tokens_in": 100, "tokens_out": 50,
                    }
                },
            },
            "counterfactual": None,
        },
    )
    job = {
        "id": "job_model_receipt",
        "status": "complete",
        "source": "harness",
        "accounting_owned": True,
        "accounting_scope": "marionette",
        "created_at": "2026-08-20T00:00:00+00:00",
    }

    code, payload = get_economics({}, _svc(jobs=[job]))

    assert code == 200
    assert isinstance(payload, dict)
    assert payload["recent_jobs"][0]["models"] == [{
        "model_id": "composer-2", "billing": "metered", "calls": 1,
        "tokens_in": 100, "tokens_out": 50,
    }]


def test_visibility_only_job_listed_but_omitted_from_owned_totals(monkeypatch):
    jobs = [
        {
            "id": "job_owned_1",
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
        "job_owned_1": _JobCost(usd=1.25, model="owned-model"),
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
    assert rows["job_owned_1"]["actual_marginal_usd"] == 1.25
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
            "id": "job_here",
            "status": "complete",
            "source": "harness",
            "accounting_owned": True,
            "accounting_scope": "marionette",
            "session_id": "sess-1",
            "created_at": "2026-08-20T00:02:00+00:00",
        },
        {
            "id": "job_other_session",
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
    assert isinstance(payload, dict)
    assert payload["scope"] == "conversation"
    assert payload["savings_scope"] == "repo"
    assert payload["savings"] is None
    assert payload["counterfactual"] == {
        "reference_model_id": "anthropic/claude-opus-4",
        "reference_priced": True,
        "naive_cost_usd": 4.0,
        "actual_cost_usd": 1.25,
        "avoided_usd": 2.75,
        "tasks": 1,
        "jobs": 1,
        "measured_cost_usd": 1.25,
        "estimated_cost_usd": 0.0,
        "spend_basis": "measured_usage_x_registry_price",
        "label": "list-price vs the named reference model, not a cash refund",
    }
    assert payload["counterfactual_source"] == "job_financial_reports"
    assert payload["all_projects"] is False
    assert reports == []
    assert [row["job_id"] for row in payload["recent_jobs"]] == ["job_here"]
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


def test_conversation_reads_session_id_from_label(monkeypatch):
    jobs = [
        {
            "id": "from-label",
            "status": "complete",
            "source": "harness",
            "accounting_owned": True,
            "accounting_scope": "marionette",
            "label": '{"session_id": "sess-1"}',
            "created_at": "2026-08-20T00:06:00+00:00",
        },
    ]
    _patch_pm(monkeypatch)
    code, payload = get_economics({"scope": ["conversation"]}, _svc(jobs=jobs))
    assert code == 200
    assert [row["job_id"] for row in payload["recent_jobs"]] == ["from-label"]


def test_window30_drops_jobs_older_than_thirty_days(monkeypatch):
    jobs = [
        {
            "id": "fresh",
            "status": "complete",
            "source": "harness",
            "accounting_owned": True,
            "accounting_scope": "marionette",
            "created_at": "2026-08-19T00:00:00+00:00",
        },
        {
            "id": "stale",
            "status": "complete",
            "source": "harness",
            "accounting_owned": True,
            "accounting_scope": "marionette",
            "created_at": "2026-06-01T00:00:00+00:00",
        },
    ]
    _patch_pm(monkeypatch)
    code, payload = get_economics({"scope": ["window30"]}, _svc(jobs=jobs))
    assert code == 200
    assert [row["job_id"] for row in payload["recent_jobs"]] == ["fresh"]


def test_all_projects_caps_extra_store_opens(tmp_path, monkeypatch):
    extras = []
    for idx in range(40):
        path = tmp_path / f"proj-{idx}"
        path.mkdir()
        extras.append(path)
    opened = []
    reports, opened = _patch_pm(monkeypatch, extra_dirs=extras, opened=opened)
    code, payload = get_economics({"scope": ["all_projects"]}, _svc())
    assert code == 200
    assert payload["all_projects"] is True
    assert len(opened) == 32
    assert opened[0] == ("sqlite", "/tmp/pm-primary")
    assert reports[0]["window_days"] is None


def test_unpriced_job_cost_is_unknown_not_measured_zero(monkeypatch):
    jobs = [
        {
            "id": "job_empty_price",
            "status": "complete",
            "source": "harness",
            "accounting_owned": True,
            "accounting_scope": "marionette",
            "created_at": "2026-08-20T00:00:00+00:00",
        },
    ]
    _patch_pm(monkeypatch)
    monkeypatch.setattr(
        "puppetmaster.cost.price_job",
        lambda arts, reg: _JobCost(
            usd=0.0,
            priced_tasks=0,
            unpriced_tasks=1,
            measured_runs=0,
            estimated_runs=0,
        ),
    )
    code, payload = get_economics({"scope": ["repo"]}, _svc(jobs=jobs))
    assert code == 200
    row = payload["recent_jobs"][0]
    assert row["actual_marginal_usd"] is None
    assert row["measured_cost_usd"] is None
    assert row["cost_basis"] == "unknown"
    assert payload["owned_actual_marginal_usd"] is None


def test_get_economics_all_projects_period_30_opens_extra_dirs(tmp_path, monkeypatch):
    extra = tmp_path / "other-project"
    extra.mkdir()
    opened = []
    reports, opened = _patch_pm(monkeypatch, extra_dirs=[extra], opened=opened)
    code, payload = get_economics(
        {"scope": ["all_projects"], "period": ["30"]},
        _svc(),
    )
    assert code == 200
    assert payload["all_projects"] is True
    assert payload["window_days"] == 30.0
    assert reports[0]["window_days"] == 30.0
    opened_paths = [path for _backend, path in opened]
    assert "/tmp/pm-primary" in opened_paths
    assert str(extra) in opened_paths
    assert len(opened_paths) == 2


def test_get_economics_repo_period_30_primary_store_only(tmp_path, monkeypatch):
    extra = tmp_path / "other-project"
    extra.mkdir()
    opened = []
    reports, opened = _patch_pm(monkeypatch, extra_dirs=[extra], opened=opened)
    code, payload = get_economics({"scope": ["repo"], "period": ["30"]}, _svc())
    assert code == 200
    assert payload["scope"] == "repo"
    assert payload["all_projects"] is False
    assert payload["window_days"] == 30.0
    assert reports[0]["window_days"] == 30.0
    assert opened == [("sqlite", "/tmp/pm-primary")]


def test_conversation_fail_closes_missing_session(monkeypatch):
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
    ]
    _patch_pm(monkeypatch)
    code, payload = get_economics(
        {"scope": ["conversation"]},
        _svc(jobs=jobs, session_id=""),
    )
    assert code == 200
    assert payload["recent_jobs"] == []
    assert payload["owned_jobs_considered"] == 0


def test_repo_economics_drops_cross_project_jobs(monkeypatch):
    _patch_pm(monkeypatch)
    jobs = [
        {
            "id": "local",
            "status": "complete",
            "source": "harness",
            "accounting_owned": True,
            "created_at": "2026-08-20T00:00:00+00:00",
        },
        {
            "id": "foreign",
            "status": "running",
            "source": "cli",
            "cross_project": True,
            "accounting_owned": False,
            "created_at": "2026-08-20T00:01:00+00:00",
        },
    ]
    _code, repo_payload = get_economics({"scope": ["repo"]}, _svc(jobs=jobs))
    repo_ids = [row.get("job_id") for row in repo_payload["recent_jobs"]]
    assert "local" in repo_ids
    assert "foreign" not in repo_ids

    _code, all_payload = get_economics({"scope": ["all_projects"]}, _svc(jobs=jobs))
    assert isinstance(all_payload, dict)
    all_ids = [row.get("job_id") for row in all_payload["recent_jobs"]]
    assert "foreign" not in all_ids


def test_repo_scope_prices_prior_session_pm_jobs_but_session_scope_does_not(monkeypatch):
    _patch_pm(monkeypatch)
    jobs = [
        {
            "id": "job_session_a",
            "status": "complete",
            "source": "harness",
            "accounting_owned": True,
            "accounting_scope": "marionette",
            "session_id": "sess-1",
            "created_at": "2026-08-20T00:02:00+00:00",
        },
        {
            "id": "job_session_b",
            "status": "complete",
            "source": "cli",
            "accounting_owned": False,
            "accounting_scope": "visibility_only",
            "session_id": "sess-2",
            "created_at": "2026-08-20T00:01:00+00:00",
        },
        {
            "id": "job_other_repo",
            "status": "complete",
            "source": "cli",
            "accounting_owned": False,
            "accounting_scope": "visibility_only",
            "cross_project": True,
            "session_id": "sess-3",
            "created_at": "2026-08-20T00:00:00+00:00",
        },
    ]

    def project(job, _store, _registry):
        owned = bool(job.get("accounting_owned"))
        cost = 1.0 if job["id"] == "job_session_a" else 2.0
        return {
            "job_id": job["id"],
            "status": "complete",
            "source": job["source"],
            "accounting_owned": owned,
            "accounting_scope": job.get("accounting_scope"),
            "measured_cost_usd": cost if owned else None,
            "estimated_cost_usd": 0.0 if owned else None,
            "actual_marginal_usd": cost if owned else None,
            "cost_basis": "measured_usage_x_registry_price" if owned else None,
            "priced_tasks": 1 if owned else 0,
            "counterfactual": ({
                "reference_model_id": "codex/gpt-5-5",
                "reference_priced": True,
                "naive_cost_usd": cost + 1.0,
                "actual_cost_usd": cost,
                "avoided_usd": 1.0,
                "tasks": 1,
            } if owned else None),
        }

    monkeypatch.setattr("harness.api.economics._project_job_row", project)
    services = _svc(jobs=jobs, session_id="sess-1")

    _code, session_payload = get_economics({"scope": ["conversation"]}, services)
    _code, repo_payload = get_economics({"scope": ["repo"]}, services)

    assert isinstance(session_payload, dict)
    assert isinstance(repo_payload, dict)
    assert session_payload["counterfactual"]["actual_cost_usd"] == 1.0
    assert session_payload["counterfactual"]["jobs"] == 1
    assert repo_payload["counterfactual"]["actual_cost_usd"] == 3.0
    assert repo_payload["counterfactual"]["jobs"] == 2


def test_all_projects_prices_job_reports_from_each_existing_pm_store(monkeypatch):
    class Store(_Store):
        def __init__(self, job_id):
            self.job_id = job_id

        def list_jobs(self):
            return [SimpleNamespace(
                id=self.job_id,
                status="complete",
                created_at="2026-08-20T00:00:00+00:00",
                label=None,
            )]

        def list_artifacts_for_jobs(self, _job_ids):
            return []

    store_a = Store("job_project_a")
    store_b = Store("job_project_b")
    monkeypatch.setattr("harness.api.economics._build_savings", lambda *_args: None)
    monkeypatch.setattr("harness.api.economics._savings_state_dirs", lambda *_args: [])
    monkeypatch.setattr(
        "harness.api.economics._open_savings_stores",
        lambda _dirs: [store_a, store_b],
    )
    monkeypatch.setattr("puppetmaster.model_registry.load_registry", lambda: [])
    monkeypatch.setattr(
        "puppetmaster.savings.build_report",
        lambda _stores, window_days=None: None,
    )

    def project(job, _store, _registry):
        cost = 1.0 if job["id"] == "job_project_a" else 2.0
        return {
            "job_id": job["id"],
            "status": "complete",
            "source": job["source"],
            "accounting_owned": True,
            "accounting_scope": "marionette",
            "measured_cost_usd": cost,
            "estimated_cost_usd": 0.0,
            "actual_marginal_usd": cost,
            "cost_basis": "measured_usage_x_registry_price",
            "priced_tasks": 1,
            "counterfactual": {
                "reference_model_id": "codex/gpt-5-5",
                "reference_priced": True,
                "naive_cost_usd": cost + 1.0,
                "actual_cost_usd": cost,
                "avoided_usd": 1.0,
                "tasks": 1,
            },
        }

    monkeypatch.setattr("harness.api.economics._project_job_row", project)
    services = EconomicsServices(
        cfg=SimpleNamespace(repo="/project-a"),
        scoped_jobs_with_stores=lambda repo_root=None: ([{
            "id": "job_project_a",
            "status": "complete",
            "source": "harness",
            "accounting_owned": True,
            "accounting_scope": "marionette",
            "created_at": "2026-08-20T00:00:00+00:00",
        }], store_a, store_a),
        diag=lambda *args, **kwargs: None,
        active_session_id=lambda: "sess-1",
    )

    _code, payload = get_economics({"scope": ["all_projects"]}, services)

    assert isinstance(payload, dict)
    assert payload["counterfactual"]["actual_cost_usd"] == 3.0
    assert payload["counterfactual"]["jobs"] == 2
    assert {row["job_id"] for row in payload["recent_jobs"]} == {
        "job_project_a",
        "job_project_b",
    }


def test_headline_aggregates_same_job_reports_as_recent_rows(monkeypatch):
    jobs = [
        {
            "id": f"job_{idx:02d}",
            "status": "complete",
            "source": "harness",
            "accounting_owned": True,
            "accounting_scope": "marionette",
            "created_at": f"2026-08-{idx + 1:02d}T00:00:00+00:00",
        }
        for idx in range(13)
    ]
    _patch_pm(monkeypatch)

    def project(job, store, registry):
        return {
            "job_id": job["id"],
            "status": "complete",
            "source": "harness",
            "accounting_owned": True,
            "accounting_scope": "marionette",
            "measured_cost_usd": 1.0,
            "estimated_cost_usd": 0.0,
            "actual_marginal_usd": 1.0,
            "cost_basis": "measured_usage_x_registry_price",
            "priced_tasks": 1,
            "unpriced_tasks": 0,
            "counterfactual": {
                "reference_model_id": "codex/gpt-5-5",
                "reference_priced": True,
                "naive_cost_usd": 2.0,
                "actual_cost_usd": 1.0,
                "avoided_usd": 1.0,
                "tasks": 1,
            },
        }

    monkeypatch.setattr("harness.api.economics._project_job_row", project)
    code, payload = get_economics({"scope": ["repo"]}, _svc(jobs=jobs))

    assert code == 200
    assert isinstance(payload, dict)
    assert len(payload["recent_jobs"]) == 12
    assert payload["recent_jobs_total"] == 13
    assert payload["owned_jobs_considered"] == 13
    assert payload["counterfactual"] == {
        "reference_model_id": "codex/gpt-5-5",
        "reference_priced": True,
        "naive_cost_usd": 26.0,
        "actual_cost_usd": 13.0,
        "avoided_usd": 13.0,
        "tasks": 13,
        "jobs": 13,
        "measured_cost_usd": 13.0,
        "estimated_cost_usd": 0.0,
        "spend_basis": "measured_usage_x_registry_price",
        "label": "list-price vs the named reference model, not a cash refund",
    }
    assert sum(row["counterfactual"]["avoided_usd"] for row in payload["recent_jobs"]) == 12.0


def test_real_pm_receipt_consumer_drives_headline_and_row(monkeypatch):
    job = {
        "id": "job_real_receipt",
        "status": "complete",
        "source": "harness",
        "accounting_owned": True,
        "accounting_scope": "marionette",
        "created_at": "2026-08-20T00:00:00+00:00",
    }
    _patch_pm(monkeypatch)
    monkeypatch.setattr(
        "harness.financial_receipt.load_pm_cost_report",
        lambda store, job_id, registry=None: {
            "job_id": job_id,
            "total_estimated_cost_usd": 0.5,
            "actual_cost": {
                "cost_basis": "measured_usage_x_registry_price",
                "total_marginal_cost_usd": 1.25,
                "measured_cost_usd": 1.25,
                "estimated_cost_usd": 0.0,
                "measured_runs": 1,
                "estimated_runs": 0,
                "priced_tasks": 1,
                "unpriced_tasks": 0,
                "by_model": {
                    "composer-2": {
                        "calls": 1,
                        "tokens_in": 100,
                        "tokens_out": 50,
                        "marginal_cost_usd": 1.25,
                        "billing": "metered",
                    },
                },
                "tasks": [],
            },
            "counterfactual": {
                "reference_model_id": "codex/gpt-5-5",
                "reference_priced": True,
                "naive_cost_usd": 4.0,
                "actual_cost_usd": 1.25,
                "avoided_usd": 2.75,
                "tasks": 1,
            },
        },
    )

    code, payload = get_economics({"scope": ["repo"]}, _svc(jobs=[job]))

    assert code == 200
    assert isinstance(payload, dict)
    assert payload["counterfactual_source"] == "job_financial_reports"
    assert payload["counterfactual"]["actual_cost_usd"] == 1.25
    assert payload["counterfactual"]["avoided_usd"] == 2.75
    assert payload["recent_jobs"][0]["measured_cost_usd"] == 1.25
    assert payload["recent_jobs"][0]["counterfactual"]["avoided_usd"] == 2.75


def test_owned_job_reports_skip_the_legacy_savings_plane(monkeypatch):
    job = {
        "id": "job_canonical_receipt",
        "status": "complete",
        "source": "harness",
        "accounting_owned": True,
        "accounting_scope": "marionette",
        "created_at": "2026-08-20T00:00:00+00:00",
    }
    reports, _opened = _patch_pm(monkeypatch)

    code, payload = get_economics({"scope": ["repo"]}, _svc(jobs=[job]))

    assert code == 200
    assert isinstance(payload, dict)
    assert payload["counterfactual_source"] == "job_financial_reports"
    assert reports == []


def test_supported_pm_builder_failure_does_not_reconstruct_an_economics_answer(monkeypatch):
    job = {
        "id": "job_canonical_failure",
        "status": "complete",
        "source": "harness",
        "accounting_owned": True,
        "accounting_scope": "marionette",
        "created_at": "2026-08-20T00:00:00+00:00",
    }
    _patch_pm(monkeypatch)
    direct_pricing_calls = []

    def fail_builder(*_args, **_kwargs):
        raise RuntimeError("canonical report unavailable")

    monkeypatch.setattr(
        "puppetmaster.cost.build_cost_report",
        fail_builder,
    )
    monkeypatch.setattr(
        "puppetmaster.cost.price_job",
        lambda *_args, **_kwargs: direct_pricing_calls.append(True) or _JobCost(),
    )

    code, payload = get_economics({"scope": ["repo"]}, _svc(jobs=[job]))

    assert code == 200
    assert direct_pricing_calls == []
    assert isinstance(payload, dict)
    assert payload["counterfactual"] is None
    assert payload["counterfactual_status"] == "incomplete"
    assert payload["recent_jobs"][0]["financial_error"] is True


def test_aggregate_fails_closed_on_mixed_reference_or_receipt_mismatch():
    base = {
        "accounting_owned": True,
        "priced_tasks": 1,
        "measured_cost_usd": 1.0,
        "estimated_cost_usd": 0.0,
        "counterfactual": {
            "reference_model_id": "codex/gpt-5-5",
            "reference_priced": True,
            "naive_cost_usd": 2.0,
            "actual_cost_usd": 1.0,
            "avoided_usd": 1.0,
            "tasks": 1,
        },
    }
    mixed = dict(base)
    mixed["counterfactual"] = {
        **base["counterfactual"],
        "reference_model_id": "other/frontier",
    }
    report, status = _aggregate_job_counterfactual([base, mixed])
    assert report is None
    assert status == "mixed_reference"

    mismatch = dict(base)
    mismatch["counterfactual"] = {
        **base["counterfactual"],
        "actual_cost_usd": 1.5,
    }
    report, status = _aggregate_job_counterfactual([mismatch])
    assert report is None
    assert status == "receipt_mismatch"

    report, status = _aggregate_job_counterfactual([
        {"accounting_owned": True, "financial_error": True}
    ])
    assert report is None
    assert status == "incomplete"


def test_aggregate_preserves_plan_included_basis():
    report, status = _aggregate_job_counterfactual([
        {
            "accounting_owned": True,
            "cost_basis": "plan",
            "priced_tasks": 1,
            "measured_cost_usd": 0.0,
            "estimated_cost_usd": 0.0,
            "counterfactual": {
                "reference_model_id": "codex/gpt-5-5",
                "reference_priced": True,
                "naive_cost_usd": 2.0,
                "actual_cost_usd": 0.0,
                "avoided_usd": 2.0,
                "tasks": 1,
            },
        }
    ])

    assert status == "ok"
    assert report is not None
    assert report["actual_cost_usd"] == 0.0
    assert report["spend_basis"] == "plan"


def test_cost_prefetch_uses_real_store_bulk_contract_and_keeps_every_artifact_type(tmp_path):
    from puppetmaster.models import Artifact, ArtifactType
    from puppetmaster.store_factory import create_store

    store = create_store("sqlite", str(tmp_path / "pm-state"))
    first = store.create_job("first")
    second = store.create_job("second")
    for job, artifact_type, payload in (
        (first, ArtifactType.FINDING, {"claim": "usage can live on any artifact", "tokens_in": 10}),
        (first, ArtifactType.VERIFICATION, {"check": "proof", "result": "passed"}),
        (second, ArtifactType.ROUTING, {"model_id": "cheap", "adapter": "codex", "policy": "balanced"}),
    ):
        store.save_artifact(Artifact(
            job_id=job.id,
            task_id="task-1",
            type=artifact_type,
            created_by="test",
            payload=payload,
            confidence=0.9,
            evidence=["test"],
        ))

    bulk_calls = []
    real_bulk = store.list_artifacts_for_jobs

    def counted_bulk(job_ids):
        bulk_calls.append(tuple(job_ids))
        return real_bulk(job_ids)

    store.list_artifacts_for_jobs = counted_bulk
    view = _PrefetchedArtifacts(store, [first.id, second.id])

    assert bulk_calls == [(first.id, second.id)]
    assert {artifact.type for artifact in view.list_artifacts(first.id)} == {
        ArtifactType.FINDING,
        ArtifactType.VERIFICATION,
    }


def test_cost_prefetch_does_not_rescan_per_job_when_bulk_result_is_empty(tmp_path):
    from puppetmaster.store_factory import create_store

    store = create_store("sqlite", str(tmp_path / "pm-state"))
    job = store.create_job("empty")
    bulk_calls = []
    real_bulk = store.list_artifacts_for_jobs

    def counted_bulk(job_ids):
        bulk_calls.append(tuple(job_ids))
        return real_bulk(job_ids)

    store.list_artifacts_for_jobs = counted_bulk
    view = _PrefetchedArtifacts(store, [job.id])
    store.list_artifacts = lambda job_id: (_ for _ in ()).throw(
        AssertionError("successful empty bulk read must not fall back to a per-job scan")
    )

    assert view.list_artifacts(job.id) == []
    assert bulk_calls == [(job.id,)]


def test_real_pm_builder_drives_fresh_scope_headline_and_row(tmp_path, monkeypatch):
    from puppetmaster.models import Artifact, ArtifactType
    from puppetmaster.store_factory import create_store

    store = create_store("sqlite", str(tmp_path / "pm-state"))
    created = store.create_job("freshness")
    job = {
        "id": created.id,
        "status": "running",
        "source": "harness",
        "accounting_owned": True,
        "accounting_scope": "marionette",
        "session_id": "sess-1",
        "created_at": "2026-08-20T00:00:00+00:00",
    }
    registry = [SimpleNamespace(
        id="cheap",
        adapter_model_name="cheap",
        input_per_mtok_usd=1.0,
        output_per_mtok_usd=2.0,
        billing="api",
        marginal_cost_usd=lambda tokens_in, tokens_out: (
            tokens_in + (2 * tokens_out)
        ) / 1_000_000.0,
        estimate_cost_usd=lambda tokens_in, tokens_out: (
            tokens_in + (2 * tokens_out)
        ) / 1_000_000.0,
    )]
    monkeypatch.setattr("puppetmaster.model_registry.load_registry", lambda: registry)
    services = EconomicsServices(
        cfg=SimpleNamespace(repo="/workspace"),
        scoped_jobs_with_stores=lambda repo_root=None: ([dict(job)], store, store),
        diag=lambda *a, **k: None,
        active_session_id=lambda: "sess-1",
    )

    first_code, first_payload = get_economics({"scope": ["conversation"]}, services)
    assert first_code == 200
    assert isinstance(first_payload, dict)
    assert first_payload["counterfactual_status"] == "unavailable"

    store.save_artifact(Artifact(
        job_id=created.id,
        task_id="task-1",
        type=ArtifactType.FINDING,
        created_by="worker",
        payload={
            "claim": "terminal usage",
            "model": "cheap",
            "result": "passed",
            "tokens_in": 100_000,
            "tokens_out": 50_000,
        },
        confidence=0.9,
        evidence=["test"],
    ))
    job["status"] = "complete"

    second_code, second_payload = get_economics({"scope": ["conversation"]}, services)
    assert second_code == 200
    assert isinstance(second_payload, dict)
    assert second_payload["counterfactual_source"] == "job_financial_reports"
    assert second_payload["counterfactual_status"] == "ok"
    assert second_payload["counterfactual"]["actual_cost_usd"] == 0.2
    assert second_payload["recent_jobs"][0]["status"] == "complete"
    assert second_payload["recent_jobs"][0]["measured_cost_usd"] == 0.2
