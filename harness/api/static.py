"""Legacy browser-shell static assets (peeled from ``harness.server``).

Owns GET ``/``, ``/index.html``, ``/app.js``, and ``/app.css`` bodies,
including harness-token meta injection into ``index.html``. Auth/public-path
gating stays on ``Handler``; this module only builds the response payload.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Tuple

# GET endpoints that are intentionally public (the same-origin renderer
# bootstrap assets, which must load BEFORE the page has the token to make
# authenticated calls). Everything else under /api requires the token.
PUBLIC_GET_PATHS = frozenset({"/", "/index.html", "/app.js", "/app.css"})

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
        # inject the auth token so the same-origin page can call the API
        meta = '<meta name="harness-token" content="%s">' % token
        html = html.replace("</head>", meta + "</head>", 1) if "</head>" in html else meta + html
        return 200, html, "text/html"
    if path == "/app.js":
        return 200, (web_root / "app.js").read_text(), "application/javascript"
    if path == "/app.css":
        return 200, (web_root / "app.css").read_text(), "text/css"
    return None
