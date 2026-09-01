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


def test_redact_secret_text_masks_github_pat_and_basic_auth():
    from harness.api.redaction import redact_secret_text

    raw = (
        "auth Basic dXNlcjpwYXNzd29yZA== and "
        "github_pat_11AAAAAAAAabcdefghijklmnopqrstuvwxyz and "
        "ghp_abcdefghijklmnopqrstuv"
    )
    out = redact_secret_text(raw)
    assert "dXNlcjpwYXNzd29yZA==" not in out
    assert "github_pat_11AAAAAAAAabcdefghijklmnopqrstuvwxyz" not in out
    assert "ghp_abcdefghijklmnopqrstuv" not in out
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


def test_nested_sensitive_keys_and_query_credentials_are_redacted():
    raw = {"HeAdErS": {"Authorization": "Bearer secret-value", "X": "ok"}, "nested": {"API_KEY": "abc"}, "url": "https://x.test/a?access_token=supersecret&next=ok"}
    out = redact_api_secrets(raw)
    assert out["HeAdErS"]["Authorization"] == "REDACTED"
    assert out["nested"]["API_KEY"] == "REDACTED"
    assert "supersecret" not in out["url"]


def test_auth_failure_attribution_is_bounded_and_privacy_safe():
    from harness.api.redaction import build_auth_failure_attribution

    raw = "Authentication failed https://x.test/?token=RAWSECRET Bearer RAWTOKEN\n\t"
    out = build_auth_failure_attribution(" plugin / evil\n", "https://host/path?id=RAWID", 401, raw)
    assert out["category"] == "authentication"
    assert out["remediation"] == "reauthenticate"
    assert out["http_status"] == 401
    assert len(out["consumer_kind"]) <= 40 and len(out["consumer_id"]) <= 40
    assert "RAWSECRET" not in str(out) and "RAWTOKEN" not in str(out) and "RAWID" not in str(out)
    assert build_auth_failure_attribution("x", "y", None, "ordinary failure") is None


def test_auth_failure_status_and_forbidden_classification():
    from harness.api.redaction import build_auth_failure_attribution

    assert build_auth_failure_attribution("api", "one", 403, "denied")["remediation"] == "check_permissions"
    assert build_auth_failure_attribution("api", "one", None, "credentials rejected")["category"] == "authentication"


def test_auth_failure_infers_unambiguous_textual_http_status():
    from harness.api.redaction import build_auth_failure_attribution

    auth = build_auth_failure_attribution("api", "one", None, "HTTP 401: login required")
    forbidden = build_auth_failure_attribution("api", "two", None, "status: 403 access denied")
    assert auth["http_status"] == 401
    assert auth["category"] == "authentication"
    assert auth["remediation"] == "reauthenticate"
    assert forbidden["http_status"] == 403
    assert forbidden["category"] == "forbidden"
    assert forbidden["remediation"] == "check_permissions"


def test_auth_failure_does_not_infer_status_from_arbitrary_numbers():
    from harness.api.redaction import build_auth_failure_attribution

    for text in ("item401", "port 4010", "count=401"):
        assert build_auth_failure_attribution("api", "one", None, text) is None


def test_explicit_auth_status_wins_over_textual_status():
    from harness.api.redaction import build_auth_failure_attribution

    out = build_auth_failure_attribution("api", "one", 401, "status: 403 access denied")
    assert out["http_status"] == 401
    assert out["category"] == "authentication"
    assert out["remediation"] == "reauthenticate"
