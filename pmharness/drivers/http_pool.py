"""Keep-alive HTTP(S) connections for provider calls.

``urllib.request.urlopen`` opens a new TCP + TLS connection for every request
and sends ``Connection: close``. Every chat turn and every tool step inside a
turn paid DNS, TCP and a TLS handshake (about 55 ms per request against the
major providers on a good network, more elsewhere) before the prompt was sent.

``urlopen(req, timeout)`` here is a drop-in for the drivers' call sites: same
response object (``http.client.HTTPResponse``), same ``HTTPError`` on 4xx/5xx,
same socket layering for the drivers' timeout and cancel helpers. It reuses
an idle connection to the same origin when one is provably healthy.

Safety rules:
- A connection returns to the pool only after its response body was read to
  its framed end and the server did not ask to close it.
- A pooled connection is checked before reuse: any readable byte or EOF means
  the peer closed it or a driver shut it down, so it is discarded. There is no
  automatic resend of a request, so a POST can never be billed twice.
- Proxies, redirects and any replaced ``urllib.request.urlopen`` (test seams)
  go through urllib unchanged.
"""

from __future__ import annotations

import http.client
import io
import socket
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

_ORIGINAL_URLOPEN = urllib.request.urlopen
_IDLE_MAX_S = 45.0
_IDLE_PER_ORIGIN = 4

_lock = threading.Lock()
_idle: dict[tuple[str, str, int], list[tuple[float, http.client.HTTPConnection]]] = {}
_ssl_context: Optional[ssl.SSLContext] = None
_stats = {"opened": 0, "reused": 0}


def stats() -> dict:
    with _lock:
        return dict(_stats, idle=sum(len(v) for v in _idle.values()))


def reset() -> None:
    """Close every idle connection (tests and process teardown)."""
    with _lock:
        pools = list(_idle.values())
        _idle.clear()
    for pool in pools:
        for _at, conn in pool:
            conn.close()


def _context() -> ssl.SSLContext:
    global _ssl_context
    if _ssl_context is None:
        # urllib builds (and loads the CA store into) a new context per call.
        _ssl_context = ssl.create_default_context()
    return _ssl_context


def _healthy(conn: http.client.HTTPConnection) -> bool:
    sock = conn.sock
    if sock is None:
        return False
    # Peek one raw byte without blocking. An idle keep-alive socket has nothing
    # to read ("would block"). EOF means the peer closed it or the read side was
    # shut down (POSIX); an error means the same on Windows (WSAESHUTDOWN, where
    # select() does not report a shut-down socket readable); stray bytes mean
    # the stream is out of step. Only "would block" is reusable. The raw
    # socket.recv bypasses the TLS layer, which rejects MSG_PEEK.
    try:
        previous = sock.gettimeout()
    except OSError:
        return False
    try:
        sock.settimeout(0)
        socket.socket.recv(sock, 1, socket.MSG_PEEK)
    except BlockingIOError:
        return True
    except (OSError, ValueError):
        return False
    finally:
        try:
            sock.settimeout(previous)
        except OSError:
            pass
    return False


def _checkout(key: tuple[str, str, int]) -> Optional[http.client.HTTPConnection]:
    now = time.monotonic()
    while True:
        with _lock:
            pool = _idle.get(key)
            if not pool:
                return None
            at, conn = pool.pop()
        if now - at <= _IDLE_MAX_S and _healthy(conn):
            return conn
        conn.close()


def _checkin(key: tuple[str, str, int], conn: http.client.HTTPConnection) -> None:
    with _lock:
        pool = _idle.setdefault(key, [])
        if len(pool) < _IDLE_PER_ORIGIN:
            pool.append((time.monotonic(), conn))
            return
    conn.close()


class _PooledResponse(http.client.HTTPResponse):
    """HTTPResponse that hands its connection back when fully consumed."""

    _pool_key: Optional[tuple[str, str, int]] = None
    _pool_conn: Optional[http.client.HTTPConnection] = None

    def close(self) -> None:
        # fp is released by http.client only when the body ended at its framed
        # length or terminal chunk; before that, or with will_close, the
        # connection state is unknown and must not be reused.
        finished = self.fp is None and not self.will_close
        super().close()
        conn, self._pool_conn = self._pool_conn, None
        if conn is None:
            return
        if finished:
            _checkin(self._pool_key, conn)
        else:
            conn.close()


def _proxied(scheme: str, host: str) -> bool:
    proxies = urllib.request.getproxies()
    return scheme in proxies and not urllib.request.proxy_bypass(host)


def _request_headers(req: urllib.request.Request) -> dict:
    headers = dict(req.header_items())
    lowered = {k.lower() for k in headers}
    # Match what urllib's opener would add, so providers see the same request.
    if req.data is not None and "content-type" not in lowered:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    if "user-agent" not in lowered:
        headers["User-Agent"] = "Python-urllib/%s.%s" % sys.version_info[:2]
    if "accept-encoding" not in lowered:
        headers["Accept-Encoding"] = "identity"
    return headers


def urlopen(req: urllib.request.Request, timeout: Optional[float] = None) -> Any:
    """``urllib.request.urlopen`` over reusable connections (see module doc)."""
    if urllib.request.urlopen is not _ORIGINAL_URLOPEN:
        return urllib.request.urlopen(req, timeout=timeout)
    parts = urllib.parse.urlsplit(req.full_url)
    scheme = parts.scheme.lower()
    host = parts.hostname or ""
    if scheme not in ("http", "https") or not host or _proxied(scheme, host):
        return _ORIGINAL_URLOPEN(req, timeout=timeout)
    port = parts.port or (443 if scheme == "https" else 80)
    key = (scheme, host, port)
    path = urllib.parse.urlunsplit(("", "", parts.path or "/", parts.query, ""))

    conn = _checkout(key)
    if conn is not None:
        conn.sock.settimeout(timeout)
        reused = True
    else:
        if scheme == "https":
            conn = http.client.HTTPSConnection(host, port, timeout=timeout, context=_context())
        else:
            conn = http.client.HTTPConnection(host, port, timeout=timeout)
        reused = False
    conn.response_class = _PooledResponse
    try:
        conn.request(req.get_method(), path, body=req.data, headers=_request_headers(req))
        resp = conn.getresponse()
    except BaseException:
        conn.close()
        raise
    with _lock:
        _stats["reused" if reused else "opened"] += 1
    resp._pool_key = key
    resp._pool_conn = conn
    resp.url = req.full_url
    resp.msg = resp.reason
    if 300 <= resp.status < 400:
        resp.read()
        resp.close()
        return _ORIGINAL_URLOPEN(req, timeout=timeout)
    if resp.status >= 400:
        body = resp.read()
        resp.close()
        raise urllib.error.HTTPError(req.full_url, resp.status, resp.reason, resp.headers, io.BytesIO(body))
    return resp
