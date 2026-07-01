import ipaddress

import pytest

from harness.url_safety import (
    is_safe_url,
    normalize_url_for_request,
    redact_sensitive_query_params,
)


def _patch_resolve(monkeypatch, ip):
    """Force socket.getaddrinfo to resolve any host to the given IP."""
    def fake_getaddrinfo(host, port, *args, **kwargs):
        fam = 10 if ":" in ip else 2  # AF_INET6 / AF_INET
        return [(fam, 1, 6, "", (ip, port or 0))]

    monkeypatch.setattr("harness.url_safety.socket.getaddrinfo", fake_getaddrinfo)


def test_non_http_scheme_blocked():
    ok, reason = is_safe_url("file:///etc/passwd")
    assert not ok
    assert "scheme" in reason


def test_localhost_blocked(monkeypatch):
    _patch_resolve(monkeypatch, "127.0.0.1")
    ok, reason = is_safe_url("http://localhost/admin")
    assert not ok


def test_loopback_ip_blocked(monkeypatch):
    _patch_resolve(monkeypatch, "127.0.0.1")
    ok, reason = is_safe_url("http://127.0.0.1:8080/")
    assert not ok
    assert "127.0.0.1" in reason


def test_metadata_ip_blocked(monkeypatch):
    _patch_resolve(monkeypatch, "169.254.169.254")
    ok, reason = is_safe_url("http://169.254.169.254/latest/meta-data/")
    assert not ok
    assert "metadata" in reason.lower()


def test_private_10_range_blocked(monkeypatch):
    _patch_resolve(monkeypatch, "10.0.0.5")
    ok, reason = is_safe_url("http://10.0.0.5/")
    assert not ok


def test_private_192_168_range_blocked(monkeypatch):
    _patch_resolve(monkeypatch, "192.168.1.1")
    ok, reason = is_safe_url("http://192.168.1.1/")
    assert not ok


def test_metadata_hostname_blocked(monkeypatch):
    _patch_resolve(monkeypatch, "169.254.169.254")
    ok, reason = is_safe_url("http://metadata.google.internal/computeMetadata/v1/")
    assert not ok
    assert "metadata" in reason.lower()


def test_public_url_allowed(monkeypatch):
    _patch_resolve(monkeypatch, "93.184.216.34")
    ok, reason = is_safe_url("https://example.com/page")
    assert ok, reason


def test_allow_private_escape_hatch(monkeypatch):
    monkeypatch.setenv("HARNESS_ALLOW_PRIVATE_URLS", "1")
    _patch_resolve(monkeypatch, "10.0.0.5")
    ok, reason = is_safe_url("http://10.0.0.5/")
    assert ok, reason


def test_metadata_still_blocked_with_hatch(monkeypatch):
    monkeypatch.setenv("HARNESS_ALLOW_PRIVATE_URLS", "1")
    _patch_resolve(monkeypatch, "169.254.169.254")
    ok, reason = is_safe_url("http://169.254.169.254/latest/meta-data/")
    assert not ok
    ok2, _ = is_safe_url("http://metadata.google.internal/")
    assert not ok2


def test_dns_rebind_to_private_blocked(monkeypatch):
    _patch_resolve(monkeypatch, "10.1.2.3")
    ok, reason = is_safe_url("https://evil.example.com/")
    assert not ok
    assert "resolved" in reason


def test_normalize_url_encodes_unicode():
    out = normalize_url_for_request("https://example.com/caf\u00e9 path?q=\u00e9")
    assert " " not in out
    assert out.startswith("https://example.com/")


def test_redact_sensitive_query_params():
    out = redact_sensitive_query_params(
        "https://api.example.com/x?token=abc123&q=hello&api_key=zzz"
    )
    assert "abc123" not in out
    assert "zzz" not in out
    assert "q=hello" in out
    assert "REDACTED" in out
