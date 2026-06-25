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
import cgi
import tempfile
import uuid

from .config import HarnessConfig
from .session import Session
from .conversation import ConversationalSession
from . import workspaces as _ws
from .sessions import SessionStore
from .autobudget import AutoBudget


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
_pilot = ConversationalSession(_cfg)
import tempfile as _tf
_sessions = SessionStore(os.path.join(_cfg.state_dir or _tf.gettempdir(), "harness_sessions.json"))
_UPLOAD_DIR = os.path.join(tempfile.gettempdir(), "harness-uploads")
os.makedirs(_UPLOAD_DIR, exist_ok=True)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

    def _send(self, code, body, ctype="application/json"):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self._cors()
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self):
        self.send_response(204); self._cors(); self.end_headers()

    def do_POST(self):
        u = urlparse(self.path)
        if u.path == "/api/upload":
            return self._handle_upload()
        if u.path in ("/api/workspaces/switch", "/api/workspaces/create",
                      "/api/sessions/create", "/api/sessions/switch"):
            return self._handle_post_json(u.path)
        return self._send(404, json.dumps({"error": "not found"}))

    def _read_json(self) -> dict:
        n = int(self.headers.get("Content-Length", 0) or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode() or "{}")
        except Exception:
            return {}

    def _handle_post_json(self, path):
        body = self._read_json()
        repo = _cfg.repo
        if path == "/api/workspaces/switch":
            return self._send(200, json.dumps(_ws.switch_workspace(repo, body.get("name",""),
                              allow_dirty=bool(body.get("allow_dirty")))))
        if path == "/api/workspaces/create":
            return self._send(200, json.dumps(_ws.create_workspace(repo, body.get("name",""),
                              body.get("branch") or None)))
        if path == "/api/sessions/create":
            return self._send(200, json.dumps(_sessions.create(body.get("title"))))
        if path == "/api/sessions/switch":
            return self._send(200, json.dumps(_sessions.switch(body.get("id",""))))
        return self._send(404, json.dumps({"error": "not found"}))

    def _handle_upload(self):
        ctype = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in ctype:
            return self._send(400, json.dumps({"error": "expected multipart/form-data"}))
        fs = cgi.FieldStorage(fp=self.rfile, headers=self.headers,
                              environ={"REQUEST_METHOD": "POST",
                                       "CONTENT_TYPE": ctype})
        saved = []
        items = fs.list or []
        for item in items:
            if getattr(item, "filename", None) and item.file:
                ext = os.path.splitext(item.filename)[1].lower() or ".png"
                if ext not in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
                    continue
                path = os.path.join(_UPLOAD_DIR, f"{uuid.uuid4().hex}{ext}")
                with open(path, "wb") as out:
                    out.write(item.file.read())
                saved.append({"path": path, "name": item.filename})
        return self._send(200, json.dumps({"saved": saved}))

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
                "budget": _cfg.budget, "state_dir": _session.state_dir,
                "models": _available_pilots(),
                "preflight": _session.preflight()}))
        if u.path == "/api/jobs":
            return self._send(200, json.dumps(_session.state().list_jobs()))
        if u.path == "/api/artifacts":
            q = parse_qs(u.query)
            jid = q.get("job_id", [""])[0]
            return self._send(200, json.dumps(_session.state().job_artifacts(jid)))
        if u.path == "/api/run":
            q = parse_qs(u.query)
            imgs = [p for p in q.get("images", [""])[0].split("|") if p]
            return self._stream_run(q.get("prompt", [""])[0], imgs)
        if u.path == "/api/chat":
            q = parse_qs(u.query)
            return self._stream_chat(q.get("message", [""])[0])
        if u.path == "/api/pilot":
            q = parse_qs(u.query)
            return self._swap_pilot(q.get("model", [""])[0])
        if u.path == "/api/workspaces":
            return self._send(200, json.dumps(_ws.list_workspaces(_cfg.repo)))
        if u.path == "/api/sessions":
            return self._send(200, json.dumps(_sessions.list()))
        if u.path == "/api/auto":
            q = parse_qs(u.query)
            return self._stream_auto(q.get("objective", [""])[0])
        return self._send(404, json.dumps({"error": "not found"}))

    def _stream_run(self, prompt: str, images=None):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        pre = _session.preflight()
        if pre:
            self.wfile.write(f"data: {json.dumps({'kind':'error','turn':0,'data':{'error':pre}})}\n\n".encode())
            self.wfile.write(b"data: {\"kind\": \"done\"}\n\n")
            self.wfile.flush()
            return
        try:
            for ev in _session.run(prompt, images=images or None):
                payload = json.dumps({"kind": ev.kind, "turn": ev.turn, "data": ev.data})
                self.wfile.write(f"data: {payload}\n\n".encode())
                self.wfile.flush()
            self.wfile.write(b"data: {\"kind\": \"done\"}\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _stream_auto(self, objective: str):
        """Stream the fully-auto loop (governor-bounded) over SSE."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self._cors()
        self.end_headers()
        try:
            budget = AutoBudget.from_env()
            for ev in _pilot.run_auto(objective, budget):
                payload = json.dumps({"kind": ev.kind, "data": ev.data})
                self.wfile.write(f"data: {payload}\n\n".encode())
                self.wfile.flush()
            self.wfile.write(b"data: {\"kind\": \"done\"}\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _swap_pilot(self, model: str):
        """Hot-swap the pilot model (the whole point: your key -> your pilot)."""
        global _pilot
        if not model:
            return self._send(400, json.dumps({"error": "model required"}))
        try:
            _cfg.driver = model
            _pilot = ConversationalSession(_cfg)
            return self._send(200, json.dumps({"ok": True, "driver": model}))
        except Exception as e:
            return self._send(500, json.dumps({"error": str(e)}))

    def _stream_chat(self, message: str):
        """Stream the conversational PILOT loop: prose messages + collapsible
        action cards (run_swarm) + assistant_done."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        pre = _pilot_preflight()
        if pre:
            self.wfile.write(f"data: {json.dumps({'kind':'error','data':{'error':pre}})}\n\n".encode())
            self.wfile.write(b"data: {\"kind\": \"done\"}\n\n")
            self.wfile.flush()
            return
        try:
            for ev in _pilot.send(message):
                payload = json.dumps({"kind": ev.kind, "data": ev.data})
                self.wfile.write(f"data: {payload}\n\n".encode())
                self.wfile.flush()
            self.wfile.write(b"data: {\"kind\": \"done\"}\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass


def _pilot_preflight():
    return _session.preflight()


def _available_pilots():
    """Pilot 'provider:model' specs for every provider whose key is set in the
    environment. Spans Anthropic/OpenAI/OpenRouter/Gemini/DeepSeek/Z.AI/... -- the
    user picks from whatever they actually have keys for. The current driver is
    always first so the picker shows it selected."""
    from . import providers as prov
    cur = _cfg.driver
    pilots = prov.available_pilots()
    # ensure the current driver appears first (it may already be in the list)
    ordered = [cur] + [p for p in pilots if p != cur]
    return ordered or [cur]


def serve(host: str = "127.0.0.1", port: int = 8799) -> None:
    import errno
    import sys
    # allow quick restarts without TIME_WAIT blocking the bind
    ThreadingHTTPServer.allow_reuse_address = True
    try:
        srv = ThreadingHTTPServer((host, port), Handler)
    except OSError as e:
        if e.errno == errno.EADDRINUSE:
            print(f"pm-harness: port {port} is already in use. Another harness GUI "
                  f"may be running.\n  - open the existing one at http://{host}:{port}\n"
                  f"  - or pick another port: harness gui --port {port + 1}",
                  file=sys.stderr)
            raise SystemExit(2)
        raise
    print(f"pm-harness GUI on http://{host}:{port}  (driver={_cfg.driver})")
    srv.serve_forever()


if __name__ == "__main__":
    import sys
    p = int(sys.argv[1]) if len(sys.argv) > 1 else 8799
    serve(port=p)
