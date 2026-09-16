from __future__ import annotations

from types import SimpleNamespace

from harness.api.privacy import get_privacy, post_privacy
from harness.privacy_paths import FORBIDDEN_ENV, load_forbidden_patterns


def test_privacy_api_set_add_remove(tmp_path, monkeypatch):
    monkeypatch.delenv(FORBIDDEN_ENV, raising=False)
    exported = []
    svc = SimpleNamespace(
        state_dir=lambda: str(tmp_path),
        refresh_workers=lambda patterns: exported.append(list(patterns)),
    )
    code, payload = get_privacy({}, svc)
    assert code == 200
    assert payload["forbidden_patterns"] == []

    code, payload = post_privacy({"action": "set", "patterns": [".env", "*.pem"]}, svc)
    assert code == 200
    assert payload["forbidden_patterns"] == [".env", "*.pem"]
    assert load_forbidden_patterns(str(tmp_path)) == [".env", "*.pem"]
    assert exported[-1] == [".env", "*.pem"]

    code, payload = post_privacy({"action": "remove", "pattern": ".env"}, svc)
    assert payload["forbidden_patterns"] == ["*.pem"]

    code, payload = post_privacy({"action": "add", "pattern": "secrets/**"}, svc)
    assert payload["forbidden_patterns"] == ["*.pem", "secrets/**"]

    code, payload = post_privacy({"action": "set", "patterns": "nope"}, svc)
    assert code == 400
