"""Manual rule/skill authoring endpoints + Windows-safe slug/write round-trips."""
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from harness.rule_store import RuleStore, Rule, _slug as rule_slug
from harness.skill_store import SkillStore, Skill, _slug as skill_slug
from harness.skill_distiller import distill_session


def _server():
    import harness.server as srv
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd, port, srv


def _headers(srv):
    return {"Content-Type": "application/json", "X-Harness-Token": srv._TOKEN}


def _get(port, path, srv):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        headers={"X-Harness-Token": srv._TOKEN},
        method="GET",
    )
    return urllib.request.urlopen(req, timeout=10)


def _post(port, path, body, headers):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(body).encode(),
        headers=headers,
        method="POST",
    )
    return urllib.request.urlopen(req, timeout=10)


def test_manual_add_rule_and_skill_endpoints(tmp_path, monkeypatch):
    import harness.server as srv

    monkeypatch.setattr(srv, "_skills", SkillStore(root=str(tmp_path / "skills")))
    monkeypatch.setattr(srv, "_rules", RuleStore(path=str(tmp_path / "rules.json")))

    httpd, port, srv_mod = _server()
    hdrs = _headers(srv_mod)
    try:
        rule_resp = json.loads(
            _post(port, "/api/rules/add",
                  {"text": "Never commit secrets", "scope": "global"}, hdrs).read()
        )
        assert rule_resp["ok"] is True
        assert rule_resp["state"] == "active"
        assert rule_resp["source"] == "manual"

        skill_resp = json.loads(
            _post(port, "/api/skills/add",
                  {"name": "Run verification", "description": "before done",
                   "body": "1. pytest\n2. build"}, hdrs).read()
        )
        assert skill_resp["ok"] is True
        assert skill_resp["state"] == "active"
        assert skill_resp["source"] == "manual"

        rules = json.loads(_get(port, "/api/rules", srv_mod).read())
        skills = json.loads(_get(port, "/api/skills", srv_mod).read())
        assert any(r["text"] == "Never commit secrets" and r["state"] == "active" for r in rules)
        assert any(s["name"] == "Run verification" and s["state"] == "active" for s in skills)
    finally:
        httpd.shutdown()


def test_manual_update_and_remove_endpoints(tmp_path, monkeypatch):
    import harness.server as srv

    monkeypatch.setattr(srv, "_skills", SkillStore(root=str(tmp_path / "skills")))
    monkeypatch.setattr(srv, "_rules", RuleStore(path=str(tmp_path / "rules.json")))

    httpd, port, srv_mod = _server()
    hdrs = _headers(srv_mod)
    try:
        rule = json.loads(
            _post(port, "/api/rules/add", {"text": "Use venv python"}, hdrs).read()
        )
        updated = json.loads(
            _post(port, "/api/rules/update",
                  {"slug": rule["slug"], "text": "Always use venv python", "scope": "repo"},
                  hdrs).read()
        )
        assert updated["text"] == "Always use venv python"
        assert updated["scope"] == "repo"
        rule_slug_for_remove = updated["slug"]

        skill = json.loads(
            _post(port, "/api/skills/add",
                  {"name": "Map auth", "description": "trace flow", "body": "grep auth"},
                  hdrs).read()
        )
        patched = json.loads(
            _post(port, "/api/skills/update",
                  {"slug": skill["slug"], "body": "1. grep auth\n2. read middleware"},
                  hdrs).read()
        )
        assert patched["ok"] is True
        skills_after = json.loads(_get(port, "/api/skills", srv_mod).read())
        updated_skill = next(s for s in skills_after if s["slug"] == skill["slug"])
        assert "grep auth" in updated_skill["body"]

        rm_rule = json.loads(_post(port, "/api/rules/remove", {"slug": rule_slug_for_remove}, hdrs).read())
        rm_skill = json.loads(_post(port, "/api/skills/remove", {"slug": skill["slug"]}, hdrs).read())
        assert rm_rule["ok"] is True
        assert rm_skill["ok"] is True

        rules = json.loads(_get(port, "/api/rules", srv_mod).read())
        skills = json.loads(_get(port, "/api/skills", srv_mod).read())
        assert not any(r["slug"] == rule_slug_for_remove for r in rules)
        assert not any(s["slug"] == skill["slug"] for s in skills)
    finally:
        httpd.shutdown()


@pytest.mark.parametrize("name", [
    "CON",
    "con.txt",
    "PRN",
    "COM1",
    "LPT9:",
    'foo<>|?*"/\\',
    " trailing dots... ",
])
def test_slug_windows_safe(name):
    slug = skill_slug(name)
    assert slug
    assert "/" not in slug and "\\" not in slug
    assert slug.upper().split(".")[0] not in {
        "CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }
    assert rule_slug(name)


def test_slug_writes_valid_file_on_windows(tmp_path):
    store = SkillStore(root=str(tmp_path))
    weird = Skill(name="CON", description="reserved device name", body="steps", state="active")
    path = store.save(weird)
    assert path.exists()
    got = store.get(weird.slug)
    assert got and got.name == "CON"


class _Pilot:
    def __init__(self, text):
        self._t = text

    def complete(self, prompt, *, system=None):
        class R:
            text = self._t
        return R()


def test_distiller_pending_approve_active_roundtrip(tmp_path):
    store = SkillStore(root=str(tmp_path))
    pilot = _Pilot('{"name":"Trace SSE bug","description":"when SSE hangs","body":"1. flush"}')
    findings = [
        {"type": "finding", "headline": "SSE needs flush"},
        {"type": "decision", "headline": "use text/event-stream"},
    ]
    res = distill_session(pilot, "fix sse", findings, store)
    assert res["status"] == "proposed"
    pending = store.get(res["slug"])
    assert pending.state == "pending"

    active = store.set_state(res["slug"], "active")
    assert active.state == "active"
    listed = store.list("active")
    assert any(s.slug == res["slug"] for s in listed)
    raw = (tmp_path / "active" / f"{res['slug']}.md").read_text(encoding="utf-8")
    assert "\r\n" not in raw
    assert "flush" in raw


def test_rule_store_utf8_roundtrip(tmp_path):
    store = RuleStore(path=str(tmp_path / "rules.json"))
    store.add(Rule(text="Never use emojis 🚫", state="active", source="manual"))
    raw = (tmp_path / "rules.json").read_text(encoding="utf-8")
    assert "🚫" in raw
    assert "\r\n" not in raw
    loaded = store.list("active")[0]
    assert "🚫" in loaded.text
