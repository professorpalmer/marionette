"""Legacy browser-shell static assets (peeled from ``harness.server``).

Owns GET ``/``, ``/index.html``, ``/app.js``, and ``/app.css`` bodies.

Historically this shell injected a `<meta name="harness-token">` so an
injected web page could make authenticated API calls before the Electron
preload bridge existed.

Security hardening removes that default token-in-DOM behavior. If you need
the legacy meta tag for a local dev/debug shell, enable it explicitly via
``HARNESS_DEV_ALLOW_TOKEN_META=1``.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional, Tuple

# GET endpoints that are intentionally public (the same-origin renderer
# bootstrap assets, which must load BEFORE the page has the token to make
# authenticated calls). Everything else under /api requires the token.
PUBLIC_GET_PATHS = frozenset({"/", "/index.html", "/app.js", "/app.css"})


def _parse_bool(val) -> bool:
    """Parse a boolean from environment value, command-line arg, or Python bool.
    
    Strict semantics: only explicit true values (True, "1", "true", "yes", "on")
    return True. Empty strings, "0", "false", "no", "off", None, etc. return False.
    """
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.lower() in ("1", "true", "yes", "on")
    return False

StaticResponse = Tuple[int, str, str]  # status, body, content_type


def try_static_shell(
    path: str,
    *,
    web_root: Path,
    token: str,
) -> Optional[StaticResponse]:
    """Return ``(status, body, content_type)`` for a public shell path, or None."""
    if path in ("/", "/index.html"):
        html = (web_root / "index.html").read_text()
        # Optional dev-only legacy behavior: inject token in DOM so older web
        # shells can bootstrap authenticated API calls. Only enabled for strict
        # boolean true (1, true, yes, on); rejects empty, 0, false, etc.
        if _parse_bool(os.environ.get("HARNESS_DEV_ALLOW_TOKEN_META")):
            meta = '<meta name="harness-token" content="%s">' % token
            html = html.replace("</head>", meta + "</head>", 1) if "</head>" in html else meta + html
        return 200, html, "text/html"
    if path == "/app.js":
        return 200, (web_root / "app.js").read_text(), "application/javascript"
    if path == "/app.css":
        return 200, (web_root / "app.css").read_text(), "text/css"
    return None
