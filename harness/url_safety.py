"""SSRF and URL-safety hardening for the pm-harness rig.

Stdlib-only. Blocks requests to private, loopback, link-local, and reserved
IP ranges, and always blocks cloud metadata endpoints. Adapted in spirit from
Hermes url_safety / website_policy / threat_patterns, but rewritten to depend
only on the standard library.
"""
from __future__ import annotations

import ipaddress
import os
import re
import socket
import urllib.parse
from typing import Optional, Tuple

# Cloud metadata endpoints. These are ALWAYS blocked, regardless of any
# escape hatch, because they are the classic SSRF credential-theft target.
METADATA_HOSTS = {
    "metadata.google.internal",
    "metadata",
}
METADATA_IPS = {
    "169.254.169.254",
    "fd00:ec2::254",
}

# Query parameter names that may carry secrets and should be redacted when a
# URL is logged or echoed back.
_SENSITIVE_PARAM_RE = re.compile(
    r"(?:token|secret|password|passwd|pwd|api[_-]?key|apikey|access[_-]?key|"
    r"auth|authorization|session|sig|signature|credential)",
    re.IGNORECASE,
)

_REDACTED = "REDACTED"


def _is_truthy_value(value) -> bool:
    """Local inline of a Hermes-style truthiness check (no dependency)."""
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in {"1", "true", "yes", "on", "y", "t"}


def allow_private_urls() -> bool:
    """True when the private-range escape hatch is enabled via env."""
    return _is_truthy_value(os.environ.get("HARNESS_ALLOW_PRIVATE_URLS"))


def _ip_is_metadata(ip: ipaddress._BaseAddress) -> bool:
    return str(ip) in METADATA_IPS


def _ip_is_blocked_range(ip: ipaddress._BaseAddress) -> bool:
    return bool(
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def _check_ip(ip_str: str, allow_private: bool) -> Tuple[bool, str]:
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return True, ""
    if _ip_is_metadata(ip):
        return False, f"blocked cloud metadata IP address {ip_str}"
    if allow_private:
        return True, ""
    if _ip_is_blocked_range(ip):
        return False, f"blocked private or reserved IP address {ip_str}"
    return True, ""


def is_safe_url(url: str) -> Tuple[bool, str]:
    """Return (ok, reason).

    Blocks:
      - non-http(s) schemes
      - cloud metadata endpoints (always, even with the escape hatch)
      - private / loopback / link-local / reserved IP ranges (unless the
        HARNESS_ALLOW_PRIVATE_URLS escape hatch is set)
    Resolves the hostname via socket.getaddrinfo and checks the resolved IPs
    too, so DNS-rebinding to a private address is caught.

    Note: this function re-resolves DNS on every call and does NOT return the
    resolved IP. To close the TOCTOU DNS-rebinding window between the safety
    check and the actual HTTP request, use is_safe_url_pinned() instead.
    """
    ok, reason, _ = _is_safe_url_impl(url)
    return ok, reason


def is_safe_url_pinned(url: str) -> Tuple[bool, str, Optional[str]]:
    """Return (ok, reason, pinned_ip).

    Like is_safe_url() but also returns the first validated resolved IP address
    so the caller can connect to that IP directly, closing the TOCTOU
    DNS-rebinding gap between the safety check and the actual HTTP request.

    When *ok* is True and the hostname resolved, *pinned_ip* is the first
    validated address from DNS resolution.  The caller MUST connect to
    *pinned_ip* when making the HTTP request while keeping the original hostname
    in the Host header and (for HTTPS) TLS SNI / certificate verification.
    When the hostname is already a literal IP, *pinned_ip* is that literal.
    When the hostname does not resolve, *pinned_ip* is None (the request will
    fail naturally on connect).
    """
    return _is_safe_url_impl(url)


def _is_safe_url_impl(url: str) -> Tuple[bool, str, Optional[str]]:
    """Shared implementation for is_safe_url and is_safe_url_pinned."""
    if not url or not isinstance(url, str):
        return False, "empty or non-string URL", None

    parsed = urllib.parse.urlsplit(url.strip())
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        return False, f"blocked non-http(s) URL scheme {scheme!r}", None

    host = parsed.hostname
    if not host:
        return False, "URL has no host", None
    host_lc = host.lower().rstrip(".")

    # Metadata hostnames are always blocked.
    if host_lc in METADATA_HOSTS:
        return False, f"blocked cloud metadata host {host_lc!r}", None

    allow_private = allow_private_urls()

    # If the host is a literal IP, check it directly (covers 127.0.0.1, 10.x,
    # 192.168.x, 169.254.169.254, etc.).
    ok, reason = _check_ip(host_lc, allow_private)
    if not ok:
        return False, reason, None

    try:
        ipaddress.ip_address(host_lc)
        # Valid IP literal — pin it directly.
        return True, "", host_lc
    except ValueError:
        pass

    # "localhost" and friends resolve to loopback; the getaddrinfo pass below
    # catches them, but block the obvious name early for a clearer message.
    if host_lc in {"localhost", "ip6-localhost", "ip6-loopback"} and not allow_private:
        return False, f"blocked loopback host {host_lc!r}", None

    # Resolve the hostname and check every resolved address.
    try:
        infos = socket.getaddrinfo(host, parsed.port or None, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, socket.error, UnicodeError):
        # Cannot resolve. Do not fail open on private checks, but there is no
        # IP to inspect; let the request proceed and fail naturally on connect.
        return True, "", None

    for info in infos:
        sockaddr = info[4]
        resolved_ip = sockaddr[0]
        ok, reason = _check_ip(resolved_ip, allow_private)
        if not ok:
            return False, f"{reason} (resolved from host {host_lc!r})", None

    # First validated IP is the pin target.
    first_ip = infos[0][4][0]
    return True, "", first_ip


def normalize_url_for_request(url: str) -> str:
    """Normalize an IRI to a URI-safe form for an HTTP request.

    Encodes non-ASCII host labels via IDNA and percent-encodes non-ASCII path,
    query, and fragment characters. Best-effort: returns the input unchanged if
    it cannot be parsed.
    """
    if not url or not isinstance(url, str):
        return url
    try:
        parsed = urllib.parse.urlsplit(url.strip())
    except ValueError:
        return url

    host = parsed.hostname
    netloc = parsed.netloc
    if host:
        try:
            encoded_host = host.encode("idna").decode("ascii")
        except (UnicodeError, ValueError):
            encoded_host = host
        userinfo = ""
        if "@" in netloc:
            userinfo = netloc.rsplit("@", 1)[0] + "@"
        port = f":{parsed.port}" if parsed.port else ""
        netloc = f"{userinfo}{encoded_host}{port}"

    path = urllib.parse.quote(parsed.path, safe="/%:@!$&'()*+,;=~-._")
    query = urllib.parse.quote(parsed.query, safe="=&%:@!$'()*+,;/?~-._")
    fragment = urllib.parse.quote(parsed.fragment, safe="=&%:@!$'()*+,;/?~-._")

    return urllib.parse.urlunsplit((parsed.scheme, netloc, path, query, fragment))


def redact_sensitive_query_params(url: str) -> str:
    """Return the URL with the values of sensitive query params redacted."""
    if not url or not isinstance(url, str):
        return url
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError:
        return url
    if not parsed.query:
        return url

    pairs = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    redacted = [
        (key, _REDACTED if _SENSITIVE_PARAM_RE.search(key) else value)
        for key, value in pairs
    ]
    new_query = urllib.parse.urlencode(redacted)
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, new_query, parsed.fragment)
    )
