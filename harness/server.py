from __future__ import annotations

"""Harness web server: a local, zero-dependency-beyond-stdlib HTTP server that
serves the three-pane GUI and streams Session events over SSE. Cursor 3.0 /
Hermes style: left nav, center driver-loop conversation, right durable-state.

stdlib http.server only -- no FastAPI/uvicorn needed, keeps the harness
dependency-light and launchable anywhere.
"""

import json
import os
import threading
import queue
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from .config import HarnessConfig
from .session import Session


_WEB = Path(__file__).resolve().parent / "web"
# One shared session per server process (single-user local app).
_state_dir = os.environ.get("HARNESS_STATE_DIR", "")
_cfg = HarnessConfig.from_env()
if _state_dir:
    _cfg.state_dir = _state_dir

# Masker-safe live key: if HARNESS_KEY_FILE points at a file, load it into the
# expected env var for the chosen reach before the Session builds its driver.
_keyfile = os.environ.get("HARNESS_KEY_FILE", "")
if _keyfile and os.path.exists(_keyfile):
    _envvar = "OPENROUTER_API_KEY" if _cfg.reach == "openrouter" else os.environ.get("HARNESS_KEY_ENV", "")
    if _envvar:
        with open(_keyfile) as _kf:
            os.environ[_envvar] = _kf.read().strip()
_session = Session(_cfg)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path in ("/", "/index.html"):
            return self._send(200, (_WEB / "index.html").read_text(), "text/html")
        if u.path == "/app.js":
            return self._send(200, (_WEB / "app.js").read_text(),
                              "application/javascript")
        if u.path == "/app.css":
            return self._send(200, (_WEB / "app.css").read_text(), "text/css")
        if u.path == "/api/config":
            return self._send(200, json.dumps({
                "driver": _cfg.driver, "reach": _cfg.reach,
                "budget": _cfg.budget, "state_dir": _session.state_dir}))
        if u.path == "/api/jobs":
            return self._send(200, json.dumps(_session.state().list_jobs()))
        if u.path == "/api/artifacts":
            q = parse_qs(u.query)
            jid = q.get("job_id", [""])[0]
            return self._send(200, json.dumps(_session.state().job_artifacts(jid)))
        if u.path == "/api/run":
            return self._stream_run(parse_qs(u.query).get("prompt", [""])[0])
        return self._send(404, json.dumps({"error": "not found"}))

    def _stream_run(self, prompt: str):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        try:
            for ev in _session.run(prompt):
                payload = json.dumps({"kind": ev.kind, "turn": ev.turn, "data": ev.data})
                self.wfile.write(f"data: {payload}\n\n".encode())
                self.wfile.flush()
            self.wfile.write(b"data: {\"kind\": \"done\"}\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass


def serve(host: str = "127.0.0.1", port: int = 8799) -> None:
    srv = ThreadingHTTPServer((host, port), Handler)
    print(f"pm-harness GUI on http://{host}:{port}  (driver={_cfg.driver})")
    srv.serve_forever()


if __name__ == "__main__":
    import sys
    p = int(sys.argv[1]) if len(sys.argv) > 1 else 8799
    serve(port=p)
