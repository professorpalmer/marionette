"""Tests for peeled API secret redaction (skills/hooks list responses)."""

from types import SimpleNamespace

from harness.api.hooks import get_hooks
from harness.api.redaction import redact_api_secrets
from harness.api.skills import SkillsServices, get_skills


class _FakeSkill:
    def __init__(self):
        self.slug = "s1"
        self.name = "n"
        self.description = "d"
        self.state = "active"
        self.source = "manual"
        self.used_count = 0
        self.body = "Use token=super-secret-value in headers"
        self.supersedes = ""


class _FakeSkills:
    def list(self):
        return [_FakeSkill()]


def test_redact_api_secrets_masks_inline_tokens():
    raw = {"body": "export API_KEY=abc123", "nested": [{"cmd": "secret: hunter2"}]}
    out = redact_api_secrets(raw)
    assert "abc123" not in str(out)
    assert "hunter2" not in str(out)
    assert out["body"].endswith("REDACTED")
    assert "REDACTED" in out["nested"][0]["cmd"]


def test_redact_api_secrets_masks_sk_and_bearer_shapes():
    raw = "HTTP 401: Authorization Bearer sk-or-v1-deadbeefcafe0123456789 failed"
    out = redact_api_secrets(raw)
    assert "sk-or-v1-deadbeefcafe0123456789" not in out
    assert "REDACTED" in out


def test_get_skills_redacts_body_in_listing():
    svc = SkillsServices(
        skills=_FakeSkills(),
        rules=SimpleNamespace(list=lambda: []),
        memory=SimpleNamespace(list=lambda: [], total_chars=lambda: 0),
        get_pilot=lambda: SimpleNamespace(),
        memory_char_limit=1000,
    )
    code, listing = get_skills(svc)
    assert code == 200
    assert "super-secret-value" not in listing[0]["body"]
    assert "REDACTED" in listing[0]["body"]


def test_get_hooks_redacts_command_field(monkeypatch):
    import harness.hooks as hk

    monkeypatch.setattr(
        hk,
        "get_hooks",
        lambda: [{"id": "h1", "event": "preRun", "command": "echo token=xyz", "enabled": True}],
    )
    monkeypatch.setattr(hk, "ALLOWED_EVENTS", ["preRun"])
    code, payload = get_hooks()
    assert code == 200
    assert "xyz" not in payload["hooks"][0]["command"]
    assert "REDACTED" in payload["hooks"][0]["command"]
