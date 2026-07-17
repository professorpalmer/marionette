"""Characterization tests for reviews + registry API peels."""
from __future__ import annotations

import json
import threading
from types import SimpleNamespace

from harness.api.reviews import (
    ReviewServices,
    get_reviews,
    post_inline_edit,
    post_reviews_apply,
    post_reviews_dismiss,
)
from harness.api.registry import (
    RegistryServices,
    get_registry,
    get_registry_recommend,
    get_roles,
    post_pilot_validate,
    post_registry,
    post_roles,
)


# --- reviews -----------------------------------------------------------------


def _review_svc(pilot, repo="/r"):
    return ReviewServices(
        cfg=SimpleNamespace(repo=repo),
        get_pilot=lambda: pilot,
        resolve_editor_path=lambda r, p: (r, p),
        strip_markdown_fences=lambda t: t.replace("```", ""),
    )


def test_reviews_apply_dismiss_missing_id():
    pilot = SimpleNamespace(
        apply_review=lambda *a: {"ok": True},
        dismiss_review=lambda *a: True,
    )
    svc = _review_svc(pilot)
    assert post_reviews_apply({}, svc)[0] == 400
    assert post_reviews_dismiss({}, svc)[0] == 400


def test_reviews_apply_dismiss_ok():
    pilot = SimpleNamespace(
        apply_review=lambda rid, d: {"ok": True, "id": rid, "decisions": d},
        dismiss_review=lambda rid: rid == "r1",
    )
    svc = _review_svc(pilot)
    code, payload = post_reviews_apply({"id": "r1", "decisions": {"a": True}}, svc)
    assert code == 200 and payload["ok"] is True
    code2, payload2 = post_reviews_dismiss({"id": "r1"}, svc)
    assert code2 == 200 and payload2["ok"] is True


def test_get_reviews_cold_and_pending():
    cold = SimpleNamespace()
    code, listing = get_reviews(_review_svc(cold))
    assert code == 200 and listing == []

    lock = threading.Lock()
    pending = {"r1": {"id": "r1"}}
    hot = SimpleNamespace(_pending_reviews_lock=lock, _pending_reviews=pending)
    code2, listing2 = get_reviews(_review_svc(hot))
    assert code2 == 200 and listing2[0]["id"] == "r1"


def test_inline_edit_gates(tmp_path):
    pilot = SimpleNamespace(pilot=None)
    svc = _review_svc(pilot, repo=str(tmp_path / "missing"))
    assert post_inline_edit({"path": "a.py"}, svc)[0] == 400

    repo = tmp_path / "repo"
    repo.mkdir()
    svc2 = _review_svc(pilot, repo=str(repo))
    assert post_inline_edit({}, svc2)[0] == 400
    code, payload = post_inline_edit({"path": "a.py", "selection": "x"}, svc2)
    assert code == 200 and payload["ok"] is False


def test_inline_edit_ok(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()

    class _Resp:
        text = "```\nfixed\n```"
        error = None

    class _Driver:
        def complete(self, prompt, system=None):
            return _Resp()

    pilot = SimpleNamespace(pilot=_Driver())
    svc = _review_svc(pilot, repo=str(repo))
    code, payload = post_inline_edit(
        {"path": "a.py", "selection": "old", "instruction": "fix"},
        svc,
    )
    assert code == 200 and payload["ok"] is True
    assert "fixed" in payload["edit"]


# --- registry ----------------------------------------------------------------


def test_post_registry_validation(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "harness.registry_wizard.get_models_file_path",
        lambda: str(tmp_path / "models.json"),
    )
    written = {}

    def _write(path, data, chmod_mode=None):
        written["data"] = data

    monkeypatch.setattr("harness.registry_wizard.write_json_atomic", _write)

    assert post_registry({})[0] == 400
    assert post_registry({"models": [{"id": "", "adapter": "x"}]})[0] == 400
    code, payload = post_registry(
        {
            "models": [
                {"id": " m1 ", "adapter": "cursor", "capability_score": 150},
            ]
        }
    )
    assert code == 200 and payload["ok"] is True
    assert payload["models"][0]["id"] == "m1"
    assert payload["models"][0]["capability_score"] == 100
    assert written["data"]["models"][0]["id"] == "m1"


def test_get_registry_empty_and_raw(tmp_path, monkeypatch):
    path = tmp_path / "models.json"
    monkeypatch.setattr(
        "harness.registry_wizard.get_models_file_path", lambda: str(path)
    )
    code, payload = get_registry()
    assert code == 200 and payload == {"models": []}
    path.write_text('{"models":[{"id":"x"}]}', encoding="utf-8")
    code2, raw = get_registry()
    assert code2 == 200 and isinstance(raw, str) and '"id":"x"' in raw


def test_roles_roundtrip(tmp_path, monkeypatch):
    from harness.registry_wizard import REAL_BASE_SCORES

    role = next(iter(REAL_BASE_SCORES))
    path = tmp_path / "routing.json"
    monkeypatch.setattr(
        "harness.registry_wizard.get_routing_file_path", lambda: str(path)
    )
    written = {}

    def _write(p, data, chmod_mode=None):
        written["data"] = data
        path.write_text(json.dumps(data), encoding="utf-8")

    monkeypatch.setattr("harness.registry_wizard.write_json_atomic", _write)
    svc = RegistryServices(diag=lambda *a: None)

    code, bad = post_roles({"overrides": {"not-a-role": 1}}, svc)
    assert code == 400
    code, bad_pol = post_roles(
        {"overrides": {role: 50}, "routing_policy": "nope"}, svc
    )
    assert code == 400

    code, ok = post_roles(
        {"overrides": {role: 50}, "routing_policy": "cheap"}, svc
    )
    assert code == 200 and ok["routing_policy"] == "cheap"
    assert ok["overrides"][role] == 50

    code2, listing = get_roles(svc)
    assert code2 == 200
    assert listing["roles"][role] == 50
    assert listing["routing_policy"] == "cheap"


def test_pilot_validate_and_recommend(monkeypatch):
    assert post_pilot_validate({})[0] == 400
    monkeypatch.setattr(
        "harness.registry_wizard.validate_pilot_driver",
        lambda d: {"ok": True, "driver": d},
    )
    code, payload = post_pilot_validate({"driver": "anthropic:claude-opus-4-8"})
    assert code == 200 and payload["ok"] is True
    monkeypatch.setattr(
        "harness.registry_wizard.get_recommendations",
        lambda: {"picks": ["a"]},
    )
    code2, rec = get_registry_recommend()
    assert code2 == 200 and rec["picks"] == ["a"]
