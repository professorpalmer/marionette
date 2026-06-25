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
import secrets as _secrets
from pathlib import Path
from urllib.parse import urlparse, parse_qs
import cgi
import tempfile
import uuid

from .config import HarnessConfig
from .session import Session
from .conversation import ConversationalSession
from .mcp_manager import McpManager, CATALOG
from .skill_store import SkillStore
from .rule_store import RuleStore
from . import workspaces as _ws
from .sessions import SessionStore, save_transcript, load_transcript
from .autobudget import AutoBudget


_WEB = Path(__file__).resolve().parent / "web"
# One shared session per server process (single-user local app).
_state_dir = os.environ.get("HARNESS_STATE_DIR", "")
_cfg = HarnessConfig.from_env()
if _state_dir:
    _cfg.state_dir = _state_dir

# Masker-safe live key: if HARNESS_KEY_FILE points at a file, load it into the
# expected env var for the chosen reach before the Session builds its driver.
from .keys import load_api_keys_on_startup, get_api_key_status, get_env_var_for_reach, set_api_key, clear_api_key
load_api_keys_on_startup(_cfg.reach)
_session = Session(_cfg)
_pilot = ConversationalSession(_cfg)
import tempfile as _tf
_sessions = SessionStore(os.path.join(_cfg.state_dir or _tf.gettempdir(), "harness_sessions.json"))
_mcp = McpManager()
_pilot._mcp = _mcp

def _rebuild_pilot_and_session():
    global _session, _pilot
    _session = Session(_cfg)
    old_history = _pilot._history
    old_auto_distill = getattr(_pilot, "_auto_distill", False)
    _pilot = ConversationalSession(_cfg)
    _pilot._history = old_history
    _pilot._auto_distill = old_auto_distill
    _pilot._mcp = _mcp

# Startup: Restore the active/most-recent session's transcript into _pilot
if _sessions.active:
    _startup_history = load_transcript(_cfg.state_dir or _tf.gettempdir(), _sessions.active)
    if _startup_history:
        _pilot.load_history(_startup_history)

_skills = SkillStore()
_rules = RuleStore()
_UPLOAD_DIR = os.path.join(tempfile.gettempdir(), "harness-uploads")
os.makedirs(_UPLOAD_DIR, exist_ok=True)

# Per-process auth token (defense-in-depth). Written chmod-600 so the local
# client (Electron main / served page) can read it; required on mutating
# endpoints. Origin/Host validation below is the primary anti-RCE guard.
_TOKEN = os.environ.get("HARNESS_TOKEN") or _secrets.token_hex(16)
_TOKEN_FILE = os.path.join(os.path.expanduser("~/.pmharness"), "token")
try:
    os.makedirs(os.path.dirname(_TOKEN_FILE), exist_ok=True)
    with open(_TOKEN_FILE, "w") as _tf2:
        _tf2.write(_TOKEN)
    os.chmod(_TOKEN_FILE, 0o600)
except OSError:
    pass

_ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}


def _host_ok(host_header: str) -> bool:
    """Defeat DNS-rebinding: the Host must be a literal loopback name. A rebound
    attacker domain (evil.com -> 127.0.0.1) shows its own name in Host."""
    if not host_header:
        return False
    host = host_header.rsplit(":", 1)[0] if host_header.count(":") <= 1 else host_header.rsplit(":", 1)[0]
    return host in _ALLOWED_HOSTS


def _origin_ok(origin: str) -> bool:
    """A malicious webpage sends its own Origin (https://evil.com) on cross-origin
    requests -> reject. Same-origin requests omit Origin; Electron file:// sends
    'null'. Both allowed."""
    if not origin or origin == "null":
        return True
    try:
        from urllib.parse import urlparse as _up
        h = _up(origin).hostname
        return h in _ALLOWED_HOSTS
    except Exception:
        return False


def _parse_bool(val) -> bool:
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.lower() in ("1", "true", "yes", "on")
    return False


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def _cors(self):
        # No wildcard. Reflect the Origin only when it is a loopback origin, so a
        # cross-origin attacker page can never read responses.
        origin = self.headers.get("Origin", "")
        if origin and origin != "null" and _origin_ok(origin):
            self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Harness-Token")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

    def _guard(self) -> bool:
        """Reject cross-origin / rebound / unauthenticated requests. Returns True
        if the request should be BLOCKED (and sends the 403)."""
        if not _host_ok(self.headers.get("Host", "")):
            self._send(403, json.dumps({"error": "host not allowed"})); return True
        if not _origin_ok(self.headers.get("Origin", "")):
            self._send(403, json.dumps({"error": "origin not allowed"})); return True
        return False

    def _token_ok(self) -> bool:
        return self.headers.get("X-Harness-Token", "") == _TOKEN

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
        if self._guard():
            return
        if not self._token_ok():
            return self._send(403, json.dumps({"error": "missing or bad token"}))
        u = urlparse(self.path)
        if u.path == "/api/upload":
            return self._handle_upload()
        if u.path in ("/api/workspaces/switch", "/api/workspaces/create",
                      "/api/sessions/create", "/api/sessions/switch",
                      "/api/mcp/add", "/api/mcp/remove", "/api/mcp/start",
                      "/api/mcp/stop", "/api/mcp/call",
                      "/api/skills/distill", "/api/skills/approve",
                      "/api/skills/reject", "/api/skills/archive",
                      "/api/rules/approve", "/api/rules/reject",
                      "/api/settings", "/api/providers/probe",
                      "/api/registry", "/api/roles", "/api/pilot/validate"):
            return self._handle_post_json(u.path)
        return self._send(404, json.dumps({"error": "not found"}))

    def _read_json(self) -> dict:
        n = int(self.headers.get("Content-Length", 0) or 0)
        if not n:
            return {}
        data = self.rfile.read(n)
        try:
            decoded = data.decode()
        except Exception as e:
            raise json.JSONDecodeError("Unicode decode error", doc="", pos=0) from e
        return json.loads(decoded or "{}")

    def _handle_post_json(self, path):
        global _pilot
        try:
            body = self._read_json()
        except json.JSONDecodeError:
            return self._send(400, json.dumps({"error": "invalid JSON"}))
        repo = _cfg.repo
        if path == "/api/workspaces/switch":
            return self._send(200, json.dumps(_ws.switch_workspace(repo, body.get("name",""),
                              allow_dirty=_parse_bool(body.get("allow_dirty")))))
        if path == "/api/workspaces/create":
            return self._send(200, json.dumps(_ws.create_workspace(repo, body.get("name",""),
                              body.get("branch") or None)))
        if path == "/api/mcp/add":
            name = body.get("name", "")
            server = {k: body[k] for k in ("command", "args", "env", "cwd", "url", "headers") if k in body}
            _mcp.save_server(name, server)
            try:
                tools = _mcp.start_server(name)
                return self._send(200, json.dumps({"ok": True, "tools": len(tools)}))
            except Exception as e:
                return self._send(200, json.dumps({"ok": False, "error": str(e)}))
        if path == "/api/mcp/remove":
            _mcp.remove_server(body.get("name", ""))
            return self._send(200, json.dumps({"ok": True}))
        if path == "/api/mcp/start":
            try:
                tools = _mcp.start_server(body.get("name", ""))
                return self._send(200, json.dumps({"ok": True, "tools": len(tools)}))
            except Exception as e:
                return self._send(200, json.dumps({"ok": False, "error": str(e)}))
        if path == "/api/mcp/stop":
            _mcp.stop_server(body.get("name", ""))
            return self._send(200, json.dumps({"ok": True}))
        if path == "/api/mcp/call":
            args = body.get("arguments")
            if args is not None and not isinstance(args, dict):
                return self._send(400, json.dumps({"error": "arguments must be a dictionary"}))
            try:
                out = _mcp.call(body.get("tool", ""), args or {})
                return self._send(200, json.dumps({"ok": True, "result": out}))
            except Exception as e:
                return self._send(200, json.dumps({"ok": False, "error": str(e)}))
        if path == "/api/skills/distill":
            return self._send(200, json.dumps(_pilot.distill()))
        if path == "/api/skills/approve":
            sk = _skills.set_state(body.get("slug", ""), "active")
            return self._send(200, json.dumps({"ok": bool(sk)}))
        if path == "/api/skills/reject":
            _skills.set_state(body.get("slug", ""), "archived")
            return self._send(200, json.dumps({"ok": True}))
        if path == "/api/skills/archive":
            _skills.set_state(body.get("slug", ""), "archived")
            return self._send(200, json.dumps({"ok": True}))
        if path == "/api/rules/approve":
            ok = _rules.set_state(body.get("slug", ""), "active")
            return self._send(200, json.dumps({"ok": ok}))
        if path == "/api/rules/reject":
            _rules.set_state(body.get("slug", ""), "archived")
            return self._send(200, json.dumps({"ok": True}))
        if path == "/api/sessions/create":
            if _sessions.active:
                save_transcript(_cfg.state_dir or _tf.gettempdir(), _sessions.active, _pilot.export_history())
            res = _sessions.create(body.get("title"))
            _pilot.load_history([])
            return self._send(200, json.dumps(res))
        if path == "/api/sessions/switch":
            if _sessions.active:
                save_transcript(_cfg.state_dir or _tf.gettempdir(), _sessions.active, _pilot.export_history())
            res = _sessions.switch(body.get("id",""))
            if res.get("ok") and _sessions.active:
                history = load_transcript(_cfg.state_dir or _tf.gettempdir(), _sessions.active)
                _pilot.load_history(history)
            return self._send(200, json.dumps(res))
        if path == "/api/settings":
            requires_rebuild = False
            if "api_key" in body or body.get("clear_api_key") is True:
                requires_rebuild = True
            driver = body.get("driver")
            if driver is not None and driver != _cfg.driver:
                requires_rebuild = True
            if requires_rebuild:
                if not _pilot._busy.acquire(blocking=False):
                    return self._send(409, json.dumps({"error": "pilot busy, try again"}))
                _pilot._busy.release()

            if "api_key" in body:
                val = str(body["api_key"]).strip()
                if val:
                    set_api_key(_cfg.reach, val)
                    _rebuild_pilot_and_session()
            elif body.get("clear_api_key") is True:
                clear_api_key(_cfg.reach)
                _rebuild_pilot_and_session()

            driver = body.get("driver")
            if driver is not None:
                av = _available_pilots()
                if driver not in av:
                    return self._send(400, json.dumps({"error": f"Unknown or unavailable driver: {driver}"}))
                if driver != _cfg.driver:
                    try:
                        _cfg.driver = driver
                        _rebuild_pilot_and_session()
                    except Exception as e:
                        return self._send(500, json.dumps({"error": f"Failed to swap driver: {str(e)}"}))
            budget = body.get("budget")
            if budget is not None:
                try:
                    b_val = int(budget)
                    _cfg.budget = max(1, min(50, b_val))
                except (ValueError, TypeError):
                    return self._send(400, json.dumps({"error": "Invalid budget value"}))
            if "auto_distill" in body:
                ad_val = _parse_bool(body["auto_distill"])
                _pilot._auto_distill = ad_val
                os.environ["HARNESS_AUTO_DISTILL"] = "true" if ad_val else "false"

            return self._send(200, json.dumps(_get_settings_dict()))

        if path == "/api/providers/probe":
            pname = body.get("provider", "")
            from .providers import get_provider
            p = get_provider(pname)
            if not p:
                return self._send(400, json.dumps({"error": f"Unknown provider: {pname}"}))
            
            from .registry_wizard import get_provider_key, probe_provider
            key = get_provider_key(p)
            try:
                res = probe_provider(p, key)
                return self._send(200, json.dumps(res))
            except Exception as e:
                return self._send(200, json.dumps({
                    "provider": p.name,
                    "models": [{"id": m} for m in p.pilot_models],
                    "source": "static",
                    "error": str(e)
                }))

        if path == "/api/registry":
            models = body.get("models")
            if not isinstance(models, list):
                return self._send(400, json.dumps({"error": "models must be a list"}))
            
            validated_models = []
            for m in models:
                if not isinstance(m, dict):
                    return self._send(400, json.dumps({"error": "each model must be a dictionary"}))
                
                model_id = m.get("id")
                if not isinstance(model_id, str) or not model_id.strip():
                    return self._send(400, json.dumps({"error": "id must be a non-empty string"}))
                
                adapter = m.get("adapter")
                if not isinstance(adapter, str):
                    return self._send(400, json.dumps({"error": "adapter must be a string"}))
                
                try:
                    score = int(m.get("capability_score", 0))
                    score = max(0, min(100, score))
                except (ValueError, TypeError):
                    return self._send(400, json.dumps({"error": "capability_score must be an integer"}))
                
                m["id"] = model_id.strip()
                m["adapter"] = adapter
                m["capability_score"] = score
                validated_models.append(m)
                
            from .registry_wizard import get_models_file_path, write_json_atomic
            dest_path = get_models_file_path()
            try:
                write_json_atomic(dest_path, {"models": validated_models})
                return self._send(200, json.dumps({"ok": True, "models": validated_models}))
            except Exception as e:
                return self._send(500, json.dumps({"error": f"Failed to write registry: {str(e)}"}))

        if path == "/api/roles":
            overrides = body.get("overrides", {})
            policy = body.get("routing_policy")
            
            if not isinstance(overrides, dict):
                return self._send(400, json.dumps({"error": "overrides must be a dictionary"}))
            
            validated_overrides = {}
            from .registry_wizard import REAL_BASE_SCORES
            for role, score in overrides.items():
                if role not in REAL_BASE_SCORES:
                    return self._send(400, json.dumps({"error": f"Unknown role: {role}"}))
                try:
                    clamped_score = max(0, min(100, int(score)))
                    validated_overrides[role] = clamped_score
                except (ValueError, TypeError):
                    return self._send(400, json.dumps({"error": f"Invalid score for role {role}: {score}"}))
            
            if policy is not None:
                valid_policies = {"balanced", "cheap", "quality", "escalating"}
                if policy not in valid_policies:
                    return self._send(400, json.dumps({"error": f"Invalid policy: {policy}; expected one of {list(valid_policies)}"}))
            
            from .registry_wizard import get_routing_file_path, write_json_atomic
            dest_path = get_routing_file_path()
            current_data = {}
            if os.path.exists(dest_path):
                try:
                    with open(dest_path) as f:
                        current_data = json.load(f)
                except Exception:
                    pass
            
            current_overrides = current_data.get("overrides", {})
            current_overrides.update(validated_overrides)
            current_data["overrides"] = current_overrides
            
            if policy is not None:
                current_data["routing_policy"] = policy
            elif "routing_policy" not in current_data:
                current_data["routing_policy"] = "balanced"
                
            try:
                write_json_atomic(dest_path, current_data, chmod_mode=0o600)
                return self._send(200, json.dumps({"ok": True, "overrides": current_data["overrides"], "routing_policy": current_data["routing_policy"]}))
            except Exception as e:
                return self._send(500, json.dumps({"error": f"Failed to save roles config: {str(e)}"}))

        if path == "/api/pilot/validate":
            driver = body.get("driver")
            if not isinstance(driver, str):
                return self._send(400, json.dumps({"error": "driver must be a string"}))
                
            from .registry_wizard import validate_pilot_driver
            try:
                res = validate_pilot_driver(driver)
                return self._send(200, json.dumps(res))
            except Exception as e:
                return self._send(500, json.dumps({"error": str(e)}))

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
            html = (_WEB / "index.html").read_text()
            # inject the auth token so the same-origin page can call the API
            meta = '<meta name="harness-token" content="%s">' % _TOKEN
            html = html.replace("</head>", meta + "</head>", 1) if "</head>" in html else meta + html
            return self._send(200, html, "text/html")
        if u.path == "/app.js":
            return self._send(200, (_WEB / "app.js").read_text(),
                              "application/javascript")
        if u.path == "/app.css":
            return self._send(200, (_WEB / "app.css").read_text(), "text/css")
        if u.path == "/api/mcp":
            return self._send(200, json.dumps({"servers": _mcp.status(),
                "tools": [{"server": t.server, "name": t.name, "qualified": t.qualified,
                           "description": t.description} for t in _mcp.tools()]}))
        if u.path == "/api/mcp/catalog":
            return self._send(200, json.dumps({"catalog": CATALOG}))
        if u.path == "/api/skills":
            return self._send(200, json.dumps([
                {"slug": sk.slug, "name": sk.name, "description": sk.description,
                 "state": sk.state, "source": sk.source, "used_count": sk.used_count,
                 "body": sk.body}
                for sk in _skills.list()]))
        if u.path == "/api/rules":
            return self._send(200, json.dumps([
                {"slug": r.slug, "text": r.text, "scope": r.scope,
                 "state": r.state, "source": r.source}
                for r in _rules.list()]))
        if u.path == "/api/config":
            return self._send(200, json.dumps({
                "driver": _cfg.driver, "reach": _cfg.reach,
                "budget": _cfg.budget, "state_dir": _session.state_dir,
                "models": _available_pilots(), "repo": _cfg.repo,
                "preflight": _session.preflight()}))
        if u.path == "/api/settings":
            return self._send(200, json.dumps(_get_settings_dict()))
        if u.path == "/api/jobs":
            return self._send(200, json.dumps(_session.state().list_jobs()))
        if u.path == "/api/artifacts":
            q = parse_qs(u.query)
            jid = q.get("job_id", [""])[0]
            return self._send(200, json.dumps(_session.state().job_artifacts(jid)))
        # action endpoints (SSE) mutate state / spend budget -> guard them.
        if u.path in ("/api/run", "/api/chat", "/api/auto", "/api/pilot", "/api/sessions/transcript", "/api/sessions/export",
                      "/api/providers", "/api/registry", "/api/roles", "/api/registry/recommend"):
            if self._guard():
                return
            from urllib.parse import parse_qs as _pq
            qtok = _pq(u.query).get("token", [""])[0]
            if qtok != _TOKEN and self.headers.get("X-Harness-Token", "") != _TOKEN:
                return self._send(403, json.dumps({"error": "missing or bad token"}))

        if u.path == "/api/providers":
            from .registry_wizard import PROVIDERS, get_provider_key
            res = []
            for p in PROVIDERS:
                res.append({
                    "name": p.name,
                    "env_var": p.env_vars[0] if p.env_vars else "",
                    "base_url": p.base_url,
                    "has_key": get_provider_key(p) is not None,
                    "api_mode": p.api_mode
                })
            return self._send(200, json.dumps(res))

        if u.path == "/api/registry":
            from .registry_wizard import get_models_file_path
            path = get_models_file_path()
            if os.path.exists(path):
                try:
                    with open(path) as f:
                        return self._send(200, f.read())
                except Exception as e:
                    return self._send(500, json.dumps({"error": f"Failed to read registry: {str(e)}"}))
            return self._send(200, json.dumps({"models": []}))

        if u.path == "/api/roles":
            from .registry_wizard import REAL_BASE_SCORES, get_routing_file_path
            path = get_routing_file_path()
            overrides = {}
            policy = "balanced"
            if os.path.exists(path):
                try:
                    with open(path) as f:
                        data = json.load(f)
                        overrides = data.get("overrides", {})
                        policy = data.get("routing_policy", "balanced")
                except Exception:
                    pass
            
            roles_mapping = {}
            for k, v in REAL_BASE_SCORES.items():
                roles_mapping[k] = overrides.get(k, v)
                
            return self._send(200, json.dumps({
                "roles": roles_mapping,
                "policies": ["balanced", "cheap", "quality", "escalating"],
                "routing_policy": policy,
                "overrides": overrides
            }))

        if u.path == "/api/registry/recommend":
            from .registry_wizard import get_recommendations
            try:
                rec = get_recommendations()
                return self._send(200, json.dumps(rec))
            except Exception as e:
                return self._send(500, json.dumps({"error": str(e)}))
        if u.path == "/api/run":
            q = parse_qs(u.query)
            imgs = []
            upload_dir_real = os.path.realpath(_UPLOAD_DIR)
            for p in q.get("images", [""])[0].split("|"):
                if not p:
                    continue
                real_p = os.path.realpath(p)
                try:
                    if os.path.commonpath([upload_dir_real, real_p]) == upload_dir_real:
                        imgs.append(p)
                    else:
                        return self._send(400, json.dumps({"error": f"Invalid image path: {p}"}))
                except ValueError:
                    return self._send(400, json.dumps({"error": f"Invalid image path: {p}"}))
            return self._stream_run(q.get("prompt", [""])[0], imgs)
        if u.path == "/api/chat":
            q = parse_qs(u.query)
            return self._stream_chat(q.get("message", [""])[0])
        if u.path == "/api/pilot":
            q = parse_qs(u.query)
            return self._swap_pilot(q.get("model", [""])[0])
        if u.path == "/api/workspaces":
            return self._send(200, json.dumps(_ws.list_workspaces(_cfg.repo)))
        if u.path == "/api/sessions/transcript":
            q = parse_qs(u.query)
            sid = q.get("session", [None])[0] or _sessions.active or ""
            history = load_transcript(_cfg.state_dir or _tf.gettempdir(), sid)
            return self._send(200, json.dumps({"history": history}))
        if u.path == "/api/sessions/export":
            q = parse_qs(u.query)
            sid = q.get("session", [None])[0] or _sessions.active or ""
            fmt = q.get("format", ["json"])[0]
            
            meta = next((s for s in _sessions._sessions if s["id"] == sid), None)
            history = load_transcript(_cfg.state_dir or _tf.gettempdir(), sid)
            
            title = meta.get("title", "Unknown Session") if meta else "Unknown Session"
            filename_base = meta.get("title") if meta else ""
            if not filename_base:
                filename_base = sid or "session"
            
            import re
            safe_title = re.sub(r'[^a-zA-Z0-9\-_]', '_', filename_base)
            safe_title = re.sub(r'_+', '_', safe_title)
            safe_title = safe_title.strip('_-')
            if not safe_title:
                safe_title = sid or "session"
                
            if fmt == "md":
                import datetime
                import time
                created = meta.get("created") if meta else None
                created_str = datetime.datetime.fromtimestamp(created).strftime('%Y-%m-%d %H:%M:%S') if created else "Unknown"
                exported_str = datetime.datetime.fromtimestamp(time.time()).strftime('%Y-%m-%d %H:%M:%S')
                
                md_lines = []
                md_lines.append(f"# {title or 'Unknown Session'}")
                md_lines.append("")
                md_lines.append(f"**Session ID:** {sid}  ")
                md_lines.append(f"**Created:** {created_str}  ")
                md_lines.append(f"**Exported:** {exported_str}")
                md_lines.append("")
                
                for msg in history:
                    role = msg.get("role", "").capitalize()
                    content = msg.get("content", "")
                    md_lines.append(f"## {role}")
                    md_lines.append("")
                    md_lines.append(content)
                    md_lines.append("")
                
                body = "\n".join(md_lines)
                data = body.encode("utf-8")
                filename = f"{safe_title}.md"
                ctype = "text/markdown"
            else:
                import time
                created = meta.get("created") if meta else None
                export_data = {
                    "session_id": sid,
                    "title": title or "Unknown Session",
                    "created": created,
                    "exported_at": time.time(),
                    "messages": history
                }
                body = json.dumps(export_data, indent=2)
                data = body.encode("utf-8")
                filename = f"{safe_title}.json"
                ctype = "application/json"
                
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self._cors()
            self.end_headers()
            self.wfile.write(data)
            return
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
            # client (browser tab) went away -> stop the governor loop promptly
            # instead of burning budget for a gone client.
            _pilot.cancel()
        finally:
            if _sessions.active:
                save_transcript(_cfg.state_dir or _tf.gettempdir(), _sessions.active, _pilot.export_history())

    def _swap_pilot(self, model: str):
        """Hot-swap the pilot model (the whole point: your key -> your pilot)."""
        global _pilot
        if not model:
            return self._send(400, json.dumps({"error": "model required"}))
        try:
            _cfg.driver = model
            _pilot = ConversationalSession(_cfg)
            _pilot._mcp = _mcp
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
        finally:
            if _sessions.active:
                save_transcript(_cfg.state_dir or _tf.gettempdir(), _sessions.active, _pilot.export_history())


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


def _get_settings_dict():
    reach = _cfg.reach
    status = get_api_key_status(reach)
    preflight_ok = (_session.preflight() is None)
    return {
        "driver": _cfg.driver,
        "reach": reach,
        "budget": _cfg.budget,
        "models": _available_pilots(),
        "auto_distill": getattr(_pilot, "_auto_distill", False),
        "wiki_auto": getattr(_cfg, "wiki_auto", False),
        "state_dir": _session.state_dir,
        "repo": _cfg.repo,
        "has_api_key": status["has_key"],
        "api_key_masked": status["masked"],
        "masked": status["masked"],
        "key_env_var": get_env_var_for_reach(reach),
        "preflight_ok": preflight_ok,
    }


def _cleanup_marker(marker_path: str, pid: int) -> None:
    try:
        if os.path.exists(marker_path):
            with open(marker_path, "r", encoding="utf-8") as f:
                m = json.load(f)
            if m and isinstance(m, dict) and m.get("pid") == pid:
                os.remove(marker_path)
    except Exception:
        pass


def serve(host: str = "127.0.0.1", port: int = 8799, force: bool = False) -> None:
    import errno
    import sys
    import urllib.request
    import urllib.error
    import time
    import atexit

    marker_dir = os.path.expanduser("~/.pmharness")
    marker_path = os.path.join(marker_dir, "backend.json")

    if not force:
        try:
            if os.path.exists(marker_path):
                with open(marker_path, "r", encoding="utf-8") as f:
                    m = json.load(f)
                if m and isinstance(m, dict) and m.get("port"):
                    m_port = m["port"]
                    try:
                        url = f"http://127.0.0.1:{m_port}/api/config"
                        with urllib.request.urlopen(url, timeout=2.0) as resp:
                            if resp.status == 200:
                                print(f"pm-harness already running at http://{host}:{m_port} — reusing")
                                return
                    except Exception:
                        pass
        except Exception:
            pass

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

    port = srv.server_address[1]

    try:
        os.makedirs(marker_dir, exist_ok=True)
        with open(marker_path, "w", encoding="utf-8") as f:
            json.dump({
                "port": port,
                "pid": os.getpid(),
                "at": int(time.time() * 1000)
            }, f)
    except Exception:
        pass

    print(f"pm-harness GUI on http://{host}:{port}  (driver={_cfg.driver})")
    # SECURITY/RESOURCE: ensure spawned MCP child processes are reaped on exit
    # (Ctrl-C, SIGTERM, SystemExit) instead of being orphaned.
    import signal
    atexit.register(_mcp.stop_all)
    atexit.register(_cleanup_marker, marker_path, os.getpid())

    def _graceful(signum, frame):
        try:
            _mcp.stop_all()
        finally:
            _cleanup_marker(marker_path, os.getpid())
            raise SystemExit(0)
    for _sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(_sig, _graceful)
        except (ValueError, OSError):
            pass  # not on the main thread (e.g. under tests) -- atexit still covers it
    try:
        srv.serve_forever()
    finally:
        _mcp.stop_all()
        _cleanup_marker(marker_path, os.getpid())


if __name__ == "__main__":
    import sys
    p = int(sys.argv[1]) if len(sys.argv) > 1 else 8799
    serve(port=p)
