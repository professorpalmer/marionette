import ipaddress
import socket

import pytest

from harness.url_safety import (
    _strip_zone_id,
    is_safe_browser_url,
    is_safe_url,
    is_safe_url_pinned,
    normalize_url_for_request,
    redact_sensitive_query_params,
    sanitize_url_for_display,
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


@pytest.mark.parametrize("resolved_ip,url,expected_ok,reason_substr", [
    pytest.param("127.0.0.1", "http://localhost/admin", False, None, id="localhost"),
    pytest.param("127.0.0.1", "http://127.0.0.1:8080/", False, "127.0.0.1", id="loopback_ip"),
    pytest.param("10.0.0.5", "http://10.0.0.5/", False, None, id="private_10_range"),
    pytest.param("192.168.1.1", "http://192.168.1.1/", False, None, id="private_192_168_range"),
    pytest.param("10.1.2.3", "https://evil.example.com/", False, "resolved", id="dns_rebind_to_private"),
    pytest.param("100.64.0.1", "http://100.64.0.1/", False, "100.64.0.1", id="cgnat"),
    # 100.63.255.255 and 100.128.0.0 are outside 100.64.0.0/10.
    pytest.param("100.63.255.255", "http://100.63.255.255/", True, None, id="cgnat_boundary_below"),
    pytest.param("100.128.0.0", "http://100.128.0.0/", True, None, id="cgnat_boundary_above"),
])
def test_is_safe_url_table(monkeypatch, resolved_ip, url, expected_ok, reason_substr):
    _patch_resolve(monkeypatch, resolved_ip)
    ok, reason = is_safe_url(url)
    assert ok == expected_ok, reason
    if reason_substr is not None:
        assert reason_substr in reason


def test_metadata_ip_blocked(monkeypatch):
    _patch_resolve(monkeypatch, "169.254.169.254")
    ok, reason = is_safe_url("http://169.254.169.254/latest/meta-data/")
    assert not ok
    assert "metadata" in reason.lower()


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


def test_sanitize_url_for_display_redacts_query_params():
    out = sanitize_url_for_display("https://x.example/?token=secret&q=ok")
    assert "secret" not in out
    assert "token=REDACTED" in out
    assert "q=ok" in out


def test_sanitize_url_for_display_redacts_userinfo():
    out = sanitize_url_for_display("https://alice:supersecret@example.com/doc.pdf")
    assert "supersecret" not in out
    assert "alice:REDACTED@" in out
    assert "example.com/doc.pdf" in out


def test_strip_zone_id():
    assert _strip_zone_id("fe80::1%eth0") == "fe80::1"
    assert _strip_zone_id("1.1.1.1") == "1.1.1.1"
    assert _strip_zone_id("") == ""


# -- is_safe_url_pinned tests (TOCTOU DNS-rebinding fix) ---------------------

def test_pinned_public_url(monkeypatch):
    _patch_resolve(monkeypatch, "93.184.216.34")
    ok, reason, pinned = is_safe_url_pinned("https://example.com/page")
    assert ok, reason
    assert pinned == "93.184.216.34"


def test_pinned_ip_literal():
    ok, reason, pinned = is_safe_url_pinned("http://93.184.216.34/")
    assert ok, reason
    assert pinned == "93.184.216.34"


@pytest.mark.parametrize("resolved_ip,url", [
    pytest.param(None, "http://10.0.0.5/", id="ip_literal_private"),
    pytest.param(None, "http://169.254.169.254/latest/meta-data/", id="metadata_ip"),
    pytest.param(None, "file:///etc/passwd", id="non_http_scheme"),
    pytest.param("169.254.169.254", "http://metadata.google.internal/computeMetadata/v1/",
                 id="metadata_hostname"),
    pytest.param("127.0.0.1", "http://127.0.0.1:8080/", id="loopback_ip"),
    pytest.param("127.0.0.1", "http://localhost/admin", id="localhost"),
])
def test_is_safe_url_pinned_blocked_table(monkeypatch, resolved_ip, url):
    if resolved_ip is not None:
        _patch_resolve(monkeypatch, resolved_ip)
    ok, reason, pinned = is_safe_url_pinned(url)
    assert not ok
    assert pinned is None


def test_pinned_dns_rebind_to_private_blocked(monkeypatch):
    _patch_resolve(monkeypatch, "10.1.2.3")
    ok, reason, pinned = is_safe_url_pinned("https://evil.example.com/")
    assert not ok
    assert pinned is None
    assert "resolved" in reason


def test_pinned_escape_hatch(monkeypatch):
    monkeypatch.setenv("HARNESS_ALLOW_PRIVATE_URLS", "1")
    _patch_resolve(monkeypatch, "10.0.0.5")
    ok, reason, pinned = is_safe_url_pinned("http://10.0.0.5/")
    assert ok, reason
    assert pinned == "10.0.0.5"


def test_pinned_metadata_still_blocked_with_hatch(monkeypatch):
    monkeypatch.setenv("HARNESS_ALLOW_PRIVATE_URLS", "1")
    _patch_resolve(monkeypatch, "169.254.169.254")
    ok, reason, pinned = is_safe_url_pinned("http://169.254.169.254/latest/meta-data/")
    assert not ok
    assert pinned is None


def test_pinned_unresolvable_host(monkeypatch):
    def fake_getaddrinfo(host, port, *args, **kwargs):
        raise socket.gaierror("Name or service not known")
    monkeypatch.setattr("harness.url_safety.socket.getaddrinfo", fake_getaddrinfo)
    ok, reason, pinned = is_safe_url_pinned("http://doesnotexist.example.com/")
    assert not ok
    assert pinned is None
    assert "could not be resolved" in reason


def test_unresolvable_host_blocked(monkeypatch):
    def fake_getaddrinfo(host, port, *args, **kwargs):
        raise socket.gaierror("Name or service not known")
    monkeypatch.setattr("harness.url_safety.socket.getaddrinfo", fake_getaddrinfo)
    ok, reason = is_safe_url("http://doesnotexist.example.com/")
    assert not ok
    assert "could not be resolved" in reason


def test_unresolvable_host_empty_dns(monkeypatch):
    def fake_getaddrinfo(host, port, *args, **kwargs):
        return []
    monkeypatch.setattr("harness.url_safety.socket.getaddrinfo", fake_getaddrinfo)
    ok, reason = is_safe_url("http://doesnotexist.example.com/")
    assert not ok
    assert "no addresses" in reason
    ok2, reason2, pinned = is_safe_url_pinned("http://doesnotexist.example.com/")
    assert not ok2
    assert pinned is None
    assert "no addresses" in reason2


def _clear_dns_cache():
    import harness.url_safety as us
    with us._DNS_CACHE_LOCK:
        us._DNS_CACHE.clear()


def test_dns_cache_reuses_within_ttl(monkeypatch):
    calls = []

    def fake_getaddrinfo(host, port, *args, **kwargs):
        calls.append(host)
        fam = 2
        return [(fam, 1, 6, "", ("93.184.216.34", port or 0))]

    monkeypatch.setattr("harness.url_safety.socket.getaddrinfo", fake_getaddrinfo)
    _clear_dns_cache()
    url = "https://dns-cache-hit.example.com/page"
    ok1, reason1 = is_safe_url(url)
    ok2, reason2 = is_safe_url(url)
    assert ok1 and ok2, (reason1, reason2)
    assert len(calls) == 1


def test_dns_cache_re_resolves_after_expiry(monkeypatch):
    calls = []
    clock = [0.0]

    def fake_getaddrinfo(host, port, *args, **kwargs):
        calls.append(host)
        fam = 2
        return [(fam, 1, 6, "", ("93.184.216.34", port or 0))]

    monkeypatch.setattr("harness.url_safety.socket.getaddrinfo", fake_getaddrinfo)
    monkeypatch.setattr("harness.url_safety.time.monotonic", lambda: clock[0])
    _clear_dns_cache()
    url = "https://dns-cache-expire.example.com/page"
    ok1, _ = is_safe_url(url)
    assert ok1
    assert len(calls) == 1
    clock[0] = 30.0
    ok2, _ = is_safe_url(url)
    assert ok2
    assert len(calls) == 1
    clock[0] = 61.0
    ok3, _ = is_safe_url(url)
    assert ok3
    assert len(calls) == 2


def test_pinned_does_not_use_dns_cache(monkeypatch):
    calls = []

    def fake_getaddrinfo(host, port, *args, **kwargs):
        calls.append(host)
        fam = 2
        return [(fam, 1, 6, "", ("93.184.216.34", port or 0))]

    monkeypatch.setattr("harness.url_safety.socket.getaddrinfo", fake_getaddrinfo)
    _clear_dns_cache()
    url = "https://dns-pinned-fresh.example.com/page"
    ok1, _, _ = is_safe_url_pinned(url)
    ok2, _, _ = is_safe_url_pinned(url)
    assert ok1 and ok2
    assert len(calls) == 2


def test_pinned_ipv6(monkeypatch):
    _patch_resolve(monkeypatch, "2606:2800:220:1:248:1893:25c8:1946")
    ok, reason, pinned = is_safe_url_pinned("https://example.com/")
    assert ok, reason
    assert pinned == "2606:2800:220:1:248:1893:25c8:1946"


# -- RFC 6598 CGNAT (100.64.0.0/10) ------------------------------------------

def test_cgnat_ipv6_mapped_blocked(monkeypatch):
    mapped = "::ffff:100.64.0.1"
    _patch_resolve(monkeypatch, mapped)
    ok, reason = is_safe_url(f"http://[{mapped}]/")
    assert not ok


def test_cgnat_allowed_with_private_hatch(monkeypatch):
    monkeypatch.setenv("HARNESS_ALLOW_PRIVATE_URLS", "1")
    _patch_resolve(monkeypatch, "100.64.0.1")
    ok, reason = is_safe_url("http://100.64.0.1/")
    assert ok, reason


# -- IPv6-mapped IPv4 SSRF bypass (::ffff:x.x.x.x) ----------------------------
# Explicit unwrap in _ip_is_blocked_range / _ip_is_metadata; do not rely on
# newer ipaddress treating mapped addresses as private/loopback.


def test_ipv6_mapped_loopback_blocked():
    """::ffff:127.0.0.1 must be blocked on all supported Python floors."""
    ok, reason = is_safe_url("http://[::ffff:127.0.0.1]/")
    assert not ok
    ok2, _, pinned = is_safe_url_pinned("http://[::ffff:127.0.0.1]/")
    assert not ok2
    assert pinned is None


def test_ipv6_mapped_metadata_blocked():
    """::ffff:169.254.169.254 must hit the metadata block (str form differs)."""
    ok, reason = is_safe_url("http://[::ffff:169.254.169.254]/latest/meta-data/")
    assert not ok
    assert "metadata" in reason.lower()
    ok2, reason2, pinned = is_safe_url_pinned(
        "http://[::ffff:169.254.169.254]/latest/meta-data/"
    )
    assert not ok2
    assert pinned is None
    assert "metadata" in reason2.lower()


def test_ipv6_mapped_private_blocked():
    ok, reason = is_safe_url("http://[::ffff:10.0.0.5]/")
    assert not ok
    ok2, _, pinned = is_safe_url_pinned("http://[::ffff:192.168.1.1]/")
    assert not ok2
    assert pinned is None


def test_ipv6_mapped_metadata_still_blocked_with_hatch(monkeypatch):
    monkeypatch.setenv("HARNESS_ALLOW_PRIVATE_URLS", "1")
    ok, reason = is_safe_url("http://[::ffff:169.254.169.254]/latest/meta-data/")
    assert not ok
    assert "metadata" in reason.lower()


def test_browser_url_allows_loopback_without_private_hatch(monkeypatch):
    monkeypatch.delenv("HARNESS_ALLOW_PRIVATE_URLS", raising=False)
    monkeypatch.delenv("HARNESS_BROWSER_ALLOW_LOOPBACK", raising=False)
    _patch_resolve(monkeypatch, "127.0.0.1")
    ok, reason = is_safe_browser_url("http://127.0.0.1:8080/")
    assert ok, reason
    ok2, reason2 = is_safe_browser_url("http://localhost/admin")
    assert ok2, reason2
    ok3, _ = is_safe_url("http://127.0.0.1:8080/")
    assert not ok3


def test_browser_url_still_blocks_metadata_and_rfc1918(monkeypatch):
    monkeypatch.delenv("HARNESS_ALLOW_PRIVATE_URLS", raising=False)
    _patch_resolve(monkeypatch, "169.254.169.254")
    ok, reason = is_safe_browser_url("http://169.254.169.254/latest/meta-data/")
    assert not ok
    assert "metadata" in reason.lower()
    _patch_resolve(monkeypatch, "10.0.0.5")
    ok2, _ = is_safe_browser_url("http://10.0.0.5/")
    assert not ok2


def test_browser_loopback_opt_out(monkeypatch):
    monkeypatch.setenv("HARNESS_BROWSER_ALLOW_LOOPBACK", "0")
    monkeypatch.delenv("HARNESS_ALLOW_PRIVATE_URLS", raising=False)
    _patch_resolve(monkeypatch, "127.0.0.1")
    ok, _ = is_safe_browser_url("http://127.0.0.1:8080/")
    assert not ok
