from __future__ import annotations

"""Harness web server: a local, zero-dependency-beyond-stdlib HTTP server that
serves the three-pane GUI and streams Session events over SSE. Cursor 3.0 /
Hermes style: left nav, center driver-loop conversation, right durable-state.

stdlib http.server only -- no FastAPI/uvicorn needed, keeps the harness
dependency-light and launchable anywhere.
"""

import json
import os
import time
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


def _get_platform_json_path() -> str:
    override = os.environ.get("TEST_PLATFORM_JSON_PATH")
    if override:
        return override
    return os.path.expanduser("~/.puppetmaster/platform.json")


def _write_platform_json_atomic(path: str, data: dict) -> None:
    dir_path = os.path.dirname(path)
    if dir_path:
        os.makedirs(dir_path, exist_ok=True)
    tmp_fd, tmp_path = tempfile.mkstemp(dir=dir_path or ".", prefix="platform_")
    try:
        with os.fdopen(tmp_fd, 'w', encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, path)
    except Exception:
        if os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        raise


def _init_platform_lock() -> None:
    path = _get_platform_json_path()
    pdata = {}
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                pdata = json.load(f)
        except Exception:
            pass
    if not isinstance(pdata, dict):
        pdata = {}
    
    if not os.path.exists(path) or "harness_initialized" not in pdata:
        if "disabled" not in pdata or not isinstance(pdata["disabled"], list):
            pdata["disabled"] = ["claude-code", "codex", "openai"]
        else:
            pdata["disabled"] = [x for x in pdata["disabled"] if x not in ("cursor", "hermes")]
        pdata["harness_initialized"] = True
        try:
            _write_platform_json_atomic(path, pdata)
        except Exception:
            pass


def _get_platform_adapters() -> dict:
    import shutil
    from .keys import get_api_key_status
    path = _get_platform_json_path()
    disabled_list = []
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                pdata = json.load(f)
                if isinstance(pdata, dict) and "disabled" in pdata and isinstance(pdata["disabled"], list):
                    disabled_list = pdata["disabled"]
        except Exception:
            pass

    adapters_config = [
        {"name": "cursor", "implement_capable": True},
        {"name": "hermes", "implement_capable": True},
        {"name": "claude-code", "implement_capable": True},
        {"name": "codex", "implement_capable": True},
        {"name": "openai", "implement_capable": False}
    ]

    adapters = []
    for cfg in adapters_config:
        name = cfg["name"]
        enabled = name not in disabled_list
        
        # Best-effort availability
        if name == "hermes":
            available = ("OPENROUTER_API_KEY" in os.environ) or get_api_key_status("openrouter")["has_key"]
            note = "Hermes via OpenRouter. Uses standard API key."
        elif name == "openai":
            available = ("OPENAI_API_KEY" in os.environ) or get_api_key_status("openai")["has_key"]
            note = "OpenAI API adapter. Note: Analysis-only, cannot drive implement tasks."
        elif name == "cursor":
            available = shutil.which("cursor") is not None
            note = "Cursor editor CLI. Run swarm/implement in a Cursor workspace."
        elif name == "claude-code":
            available = shutil.which("claude") is not None
            note = "Anthropic Claude Code. Requires 'claude' npm command in path."
        elif name == "codex":
            available = shutil.which("codex") is not None
            note = "Codex agent CLI. Requires 'codex' command in path."
        else:
            available = True
            note = ""

        adapters.append({
            "name": name,
            "enabled": enabled,
            "implement_capable": cfg["implement_capable"],
            "available": available,
            "note": note
        })
    return {"adapters": adapters}


_WEB = Path(__file__).resolve().parent / "web"
# One shared session per server process (single-user local app).
_state_dir = os.environ.get("HARNESS_STATE_DIR", "")
_cfg = HarnessConfig.from_env()
_WORKSPACE_JSON = os.path.expanduser("~/.pmharness/workspace.json")
if not os.environ.get("HARNESS_REPO") and os.path.exists(_WORKSPACE_JSON):
    try:
        with open(_WORKSPACE_JSON, "r") as _ws_f:
            _ws_data = json.load(_ws_f)
            if _ws_data.get("repo") and os.path.isdir(_ws_data["repo"]):
                _cfg.repo = _ws_data["repo"]
                os.environ["HARNESS_REPO"] = _ws_data["repo"]
    except Exception:
        pass

if _state_dir:
    _cfg.state_dir = _state_dir

# Masker-safe live key: if HARNESS_KEY_FILE points at a file, load it into the
# expected env var for the chosen reach before the Session builds its driver.
from .keys import load_api_keys_on_startup, get_api_key_status, get_env_var_for_reach, set_api_key, clear_api_key
from .wiki_config import load_wiki_config_on_startup, get_wiki_config, set_wiki_config
load_api_keys_on_startup(_cfg.reach)
load_wiki_config_on_startup()
_session = Session(_cfg)
_pilot = ConversationalSession(_cfg)
import tempfile as _tf
_sessions = SessionStore(os.path.join(_cfg.state_dir or _tf.gettempdir(), "harness_sessions.json"))
_mcp = McpManager()
from .pty_manager import PtyManager
_pty = PtyManager()
_pilot._mcp = _mcp
_init_platform_lock()

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


_codegraph_status = "unsupported"


def _index_codegraph_bg(repo_path: str):
    global _codegraph_status
    _codegraph_status = "indexing"
    try:
        import sys
        import subprocess
        proc = subprocess.Popen(
            [sys.executable, "-m", "puppetmaster", "codegraph", "init", "--index"],
            cwd=repo_path,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )

        def wait_and_update():
            global _codegraph_status
            try:
                proc.wait(timeout=600)  # max 10 mins
                if proc.returncode == 0:
                    _codegraph_status = "ready"
                else:
                    _codegraph_status = "unsupported"
            except Exception:
                _codegraph_status = "unsupported"
                try:
                    proc.kill()
                except Exception:
                    pass

        threading.Thread(target=wait_and_update, daemon=True).start()
    except Exception:
        _codegraph_status = "unsupported"


def _reindex_codegraph_bg(repo_path: str):
    global _codegraph_status
    _codegraph_status = "indexing"
    try:
        import sys
        import subprocess
        proc = subprocess.Popen(
            [sys.executable, "-m", "puppetmaster", "codegraph", "index"],
            cwd=repo_path,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )

        def wait_and_update():
            global _codegraph_status
            try:
                proc.wait(timeout=600)  # max 10 mins
                if proc.returncode == 0:
                    _codegraph_status = "ready"
                else:
                    _codegraph_status = "unsupported"
            except Exception:
                _codegraph_status = "unsupported"
                try:
                    proc.kill()
                except Exception:
                    pass

        threading.Thread(target=wait_and_update, daemon=True).start()
    except Exception:
        _codegraph_status = "unsupported"


def _get_codegraph_status(repo_path: str) -> str:
    global _codegraph_status
    if not repo_path:
        return "unsupported"
    if _codegraph_status == "indexing":
        return "indexing"

    try:
        import puppetmaster.codegraph as cg
        if os.path.isdir(os.path.join(repo_path, ".codegraph")):
            _codegraph_status = "ready"
            return "ready"
        else:
            return "unsupported"
    except Exception:
        _codegraph_status = "unsupported"
        return "unsupported"


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
                      "/api/sessions/delete", "/api/sessions/archive", "/api/sessions/rename",
                      "/api/mcp/add", "/api/mcp/remove", "/api/mcp/start",
                      "/api/mcp/stop", "/api/mcp/call",
                      "/api/skills/distill", "/api/skills/approve",
                      "/api/skills/reject", "/api/skills/archive",
                      "/api/rules/approve", "/api/rules/reject",
                      "/api/settings", "/api/providers/probe", "/api/wiki/config",
                      "/api/platform",
                      "/api/registry", "/api/roles", "/api/pilot/validate",
                      "/api/worktrees/add", "/api/worktrees/remove",
                      "/api/worktrees/prune", "/api/worktrees/max",
                      "/api/hooks/add", "/api/hooks/update", "/api/hooks/remove",
                      "/api/workspace/open", "/api/codegraph/reindex",
                      "/api/terminal/create", "/api/terminal/write",
                      "/api/terminal/resize", "/api/terminal/kill"):
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
        if path == "/api/codegraph/reindex":
            if not repo or not os.path.isdir(repo):
                return self._send(400, json.dumps({"error": "No open workspace"}))
            _reindex_codegraph_bg(repo)
            return self._send(200, json.dumps({"ok": True, "status": "indexing"}))
        if path == "/api/workspace/open":
            import subprocess
            target_repo = body.get("path", "").strip()
            if not target_repo or not os.path.isdir(target_repo):
                return self._send(400, json.dumps({"error": "Path is not an existing directory"}))

            _cfg.repo = target_repo
            os.environ["HARNESS_REPO"] = target_repo

            ws_json_path = os.path.expanduser("~/.pmharness/workspace.json")
            try:
                os.makedirs(os.path.dirname(ws_json_path), exist_ok=True)
                recents = []
                if os.path.exists(ws_json_path):
                    try:
                        with open(ws_json_path) as f:
                            recents = json.load(f).get("recents", []) or []
                    except Exception:
                        recents = []
                # prepend, dedupe (keep first occurrence), cap 8
                recents = [target_repo] + [r for r in recents if r != target_repo]
                recents = recents[:8]
                with open(ws_json_path, "w") as f:
                    json.dump({"repo": target_repo, "recents": recents}, f)
                os.chmod(ws_json_path, 0o600)
            except Exception:
                pass

            _rebuild_pilot_and_session()

            is_git = False
            branch = ""
            try:
                proc = subprocess.run(
                    ["git", "-C", target_repo, "rev-parse", "--is-inside-work-tree"],
                    capture_output=True, text=True, timeout=5
                )
                if proc.returncode == 0:
                    is_git = True
                    proc_branch = subprocess.run(
                        ["git", "-C", target_repo, "rev-parse", "--abbrev-ref", "HEAD"],
                        capture_output=True, text=True, timeout=5
                    )
                    if proc_branch.returncode == 0:
                        branch = proc_branch.stdout.strip()
            except Exception:
                pass

            if _sessions.active:
                _sessions.stamp_session(_sessions.active, target_repo, branch)

            has_codegraph = os.path.isdir(os.path.join(target_repo, ".codegraph"))
            if not has_codegraph:
                _index_codegraph_bg(target_repo)
            else:
                global _codegraph_status
                try:
                    import puppetmaster.codegraph as cg
                    _codegraph_status = "ready"
                except Exception:
                    _codegraph_status = "unsupported"

            return self._send(200, json.dumps({
                "ok": True,
                "repo": target_repo,
                "branch": branch,
                "is_git": is_git,
                "codegraph": _get_codegraph_status(target_repo)
            }))

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
            title = body.get("title") or "New session"
            repo = _cfg.repo or ""
            branch = ""
            if repo and os.path.isdir(repo):
                import subprocess
                try:
                    proc = subprocess.run(
                        ["git", "-C", repo, "rev-parse", "--is-inside-work-tree"],
                        capture_output=True, text=True, timeout=5
                    )
                    if proc.returncode == 0:
                        proc_branch = subprocess.run(
                            ["git", "-C", repo, "rev-parse", "--abbrev-ref", "HEAD"],
                            capture_output=True, text=True, timeout=5
                        )
                        if proc_branch.returncode == 0:
                            branch = proc_branch.stdout.strip()
                except Exception:
                    pass
            res = _sessions.create(title, repo=repo, branch=branch)
            _pilot.load_history([])
            
            from .hooks import run_hooks
            run_hooks("sessionStart", {"session_id": res.get("id", ""), "title": title})
            
            return self._send(200, json.dumps(res))
        if path == "/api/sessions/switch":
            if _sessions.active:
                save_transcript(_cfg.state_dir or _tf.gettempdir(), _sessions.active, _pilot.export_history())
            res = _sessions.switch(body.get("id",""))
            if res.get("ok") and _sessions.active:
                history = load_transcript(_cfg.state_dir or _tf.gettempdir(), _sessions.active)
                _pilot.load_history(history)
            return self._send(200, json.dumps(res))
        if path == "/api/sessions/delete":
            sid = body.get("session") or body.get("id") or ""
            if not sid:
                return self._send(400, json.dumps({"error": "missing session id"}))
            is_active = (_sessions.active == sid)
            
            from .hooks import run_hooks
            run_hooks("sessionEnd", {"session_id": sid})
            
            new_active = _sessions.delete(sid)
            safe_sid = "".join(c for c in sid if c.isalnum() or c in ("-", "_"))
            if safe_sid:
                state_dir = _cfg.state_dir or _tf.gettempdir()
                trans_dir = os.path.abspath(os.path.join(state_dir, "transcripts"))
                p = os.path.abspath(os.path.join(trans_dir, f"{safe_sid}.json"))
                if p.startswith(trans_dir) and os.path.exists(p):
                    try:
                        os.remove(p)
                    except Exception:
                        pass
            if is_active:
                if new_active:
                    history = load_transcript(_cfg.state_dir or _tf.gettempdir(), new_active)
                    _pilot.load_history(history)
                else:
                    _pilot.load_history([])
            return self._send(200, json.dumps({"ok": True, "active": new_active}))
        if path == "/api/sessions/archive":
            sid = body.get("session") or body.get("id") or ""
            if not sid:
                return self._send(400, json.dumps({"error": "missing session id"}))
            archived = _parse_bool(body.get("archived"))
            _sessions.archive(sid, archived)
            return self._send(200, json.dumps({"ok": True}))
        if path == "/api/sessions/rename":
            sid = body.get("session") or body.get("id") or ""
            title = body.get("title") or ""
            if not sid:
                return self._send(400, json.dumps({"error": "missing session id"}))
            if not title:
                return self._send(400, json.dumps({"error": "missing title"}))
            ok = _sessions.rename(sid, title)
            return self._send(200, json.dumps({"ok": ok}))
        if path == "/api/terminal/create":
            try:
                cwd = _cfg.repo or os.path.expanduser("~")
                cols = int(body.get("cols", 80)); rows = int(body.get("rows", 24))
                sess = _pty.create(cwd=cwd, cols=cols, rows=rows)
                return self._send(200, json.dumps({"id": sess.id, "cwd": sess._cwd}))
            except Exception as e:
                return self._send(500, json.dumps({"error": str(e)}))
        if path == "/api/terminal/write":
            sess = _pty.get(body.get("id", ""))
            if not sess:
                return self._send(404, json.dumps({"error": "no such terminal"}))
            sess.write(body.get("data", ""))
            return self._send(200, json.dumps({"ok": True}))
        if path == "/api/terminal/resize":
            sess = _pty.get(body.get("id", ""))
            if not sess:
                return self._send(404, json.dumps({"error": "no such terminal"}))
            sess.resize(int(body.get("rows", 24)), int(body.get("cols", 80)))
            return self._send(200, json.dumps({"ok": True}))
        if path == "/api/terminal/kill":
            _pty.kill(body.get("id", ""))
            return self._send(200, json.dumps({"ok": True}))
        if path == "/api/wiki/config":
            api_base = body.get("api_base")
            owner_token = body.get("owner_token")
            res = set_wiki_config(
                api_base=api_base if api_base is not None else None,
                owner_token=owner_token if owner_token is not None else None,
            )
            return self._send(200, json.dumps(res))
        if path == "/api/platform":
            name = body.get("name")
            enabled = body.get("enabled")
            if name not in ("cursor", "hermes", "claude-code", "codex", "openai"):
                return self._send(400, json.dumps({"error": f"Unknown adapter: {name}"}))
            if not isinstance(enabled, bool):
                return self._send(400, json.dumps({"error": "enabled must be a boolean"}))
            
            path_file = _get_platform_json_path()
            pdata = {}
            if os.path.exists(path_file):
                try:
                    with open(path_file, "r", encoding="utf-8") as f:
                        pdata = json.load(f)
                except Exception:
                    pass
            if not isinstance(pdata, dict):
                pdata = {}
            if "disabled" not in pdata or not isinstance(pdata["disabled"], list):
                pdata["disabled"] = []
            
            disabled_list = pdata["disabled"]
            if enabled:
                pdata["disabled"] = [x for x in disabled_list if x != name]
            else:
                if name not in disabled_list:
                    pdata["disabled"] = disabled_list + [name]
            
            try:
                _write_platform_json_atomic(path_file, pdata)
            except Exception as e:
                return self._send(500, json.dumps({"error": f"Failed to save platform.json: {str(e)}"}))
            
            return self._send(200, json.dumps(_get_platform_adapters()))
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

            reach_to_use = body.get("reach", _cfg.reach)
            if "api_key" in body:
                val = str(body["api_key"]).strip()
                if val:
                    set_api_key(reach_to_use, val)
                    _rebuild_pilot_and_session()
            elif body.get("clear_api_key") is True:
                clear_api_key(reach_to_use)
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

        if path == "/api/worktrees/add":
            from . import worktrees as _wt
            branch = body.get("branch", "").strip()
            base = body.get("base") or "HEAD"
            if not branch or branch.startswith("-") or (base and base.startswith("-")):
                return self._send(400, json.dumps({"error": "invalid branch or base name"}))
            try:
                new_wt = _wt.add_worktree(_cfg.repo, branch, base)
                _wt.cleanup_old_worktrees(_cfg.repo, _wt.get_max_worktrees())
                return self._send(200, json.dumps(new_wt))
            except ValueError as e:
                return self._send(400, json.dumps({"error": str(e)}))
            except Exception as e:
                return self._send(400, json.dumps({"error": f"Failed to add worktree: {str(e)}"}))

        if path == "/api/worktrees/remove":
            from . import worktrees as _wt
            wt_path = body.get("path", "").strip()
            force = _parse_bool(body.get("force"))
            if not wt_path:
                return self._send(400, json.dumps({"error": "missing path"}))
            try:
                _wt.remove_worktree(_cfg.repo, wt_path, force=force)
                return self._send(200, json.dumps({"ok": True}))
            except ValueError as e:
                return self._send(400, json.dumps({"error": str(e)}))
            except Exception as e:
                return self._send(400, json.dumps({"error": f"Failed to remove worktree: {str(e)}"}))

        if path == "/api/worktrees/prune":
            from . import worktrees as _wt
            try:
                _wt.prune_worktrees(_cfg.repo)
                return self._send(200, json.dumps({"ok": True}))
            except Exception as e:
                return self._send(400, json.dumps({"error": f"Failed to prune worktrees: {str(e)}"}))

        if path == "/api/worktrees/max":
            from . import worktrees as _wt
            try:
                max_val = int(body.get("max") or body.get("max_worktrees") or 25)
                _wt.set_max_worktrees(max_val)
                _wt.cleanup_old_worktrees(_cfg.repo, max_val)
                return self._send(200, json.dumps({"ok": True}))
            except (ValueError, TypeError):
                return self._send(400, json.dumps({"error": "Invalid max value"}))

        if path == "/api/hooks/add":
            from . import hooks as _hk
            event = body.get("event", "").strip()
            command = body.get("command", "").strip()
            if event not in _hk.ALLOWED_EVENTS:
                return self._send(400, json.dumps({"error": f"Invalid event. Allowed: {_hk.ALLOWED_EVENTS}"}))
            if not command:
                return self._send(400, json.dumps({"error": "Command cannot be empty"}))
            
            hooks = _hk.get_hooks()
            new_hook = {
                "id": uuid.uuid4().hex[:12],
                "event": event,
                "command": command,
                "enabled": True
            }
            hooks.append(new_hook)
            _hk.save_hooks(hooks)
            return self._send(200, json.dumps(new_hook))

        if path == "/api/hooks/update":
            from . import hooks as _hk
            hid = body.get("id", "").strip()
            if not hid:
                return self._send(400, json.dumps({"error": "missing hook id"}))
            
            hooks = _hk.get_hooks()
            hook = next((h for h in hooks if h["id"] == hid), None)
            if not hook:
                return self._send(404, json.dumps({"error": "hook not found"}))
            
            if "enabled" in body:
                hook["enabled"] = _parse_bool(body["enabled"])
            if "command" in body:
                cmd = body["command"].strip()
                if not cmd:
                    return self._send(400, json.dumps({"error": "Command cannot be empty"}))
                hook["command"] = cmd
            
            _hk.save_hooks(hooks)
            return self._send(200, json.dumps(hook))

        if path == "/api/hooks/remove":
            from . import hooks as _hk
            hid = body.get("id", "").strip()
            if not hid:
                return self._send(400, json.dumps({"error": "missing hook id"}))
            
            hooks = _hk.get_hooks()
            hooks = [h for h in hooks if h["id"] != hid]
            _hk.save_hooks(hooks)
            return self._send(200, json.dumps({"ok": True}))

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
        if u.path == "/api/workspace":
            repo = _cfg.repo
            is_git = False
            branch = ""
            if repo and os.path.isdir(repo):
                import subprocess
                try:
                    proc = subprocess.run(
                        ["git", "-C", repo, "rev-parse", "--is-inside-work-tree"],
                        capture_output=True, text=True, timeout=5
                    )
                    if proc.returncode == 0:
                        is_git = True
                        proc_branch = subprocess.run(
                            ["git", "-C", repo, "rev-parse", "--abbrev-ref", "HEAD"],
                            capture_output=True, text=True, timeout=5
                        )
                        if proc_branch.returncode == 0:
                            branch = proc_branch.stdout.strip()
                except Exception:
                    pass
            cg_status = _get_codegraph_status(repo) if repo else "unsupported"
            recents = []
            try:
                if os.path.exists(_WORKSPACE_JSON):
                    with open(_WORKSPACE_JSON) as f:
                        recents = json.load(f).get("recents", []) or []
            except Exception:
                recents = []
            return self._send(200, json.dumps({
                "repo": repo,
                "branch": branch,
                "is_git": is_git,
                "codegraph_status": cg_status,
                "recents": recents
            }))
        if u.path == "/api/codegraph":
            if self._guard():
                return
            qtok = parse_qs(u.query).get("token", [""])[0]
            if qtok != _TOKEN and self.headers.get("X-Harness-Token", "") != _TOKEN:
                return self._send(403, json.dumps({"error": "missing or bad token"}))

            repo = _cfg.repo
            if not repo or not os.path.isdir(repo):
                return self._send(200, json.dumps({
                    "indexed": False,
                    "status": "none",
                    "nodes": None,
                    "edges": None,
                    "files": None,
                    "languages": None,
                    "last_indexed": None,
                    "repo": ""
                }))

            if _codegraph_status == "indexing":
                last_indexed = None
                try:
                    import puppetmaster.codegraph as cg
                    mtime = cg.codegraph_index_mtime(repo)
                    if mtime:
                        import datetime
                        last_indexed = datetime.datetime.fromtimestamp(mtime).isoformat()
                except Exception:
                    pass

                return self._send(200, json.dumps({
                    "indexed": False,
                    "status": "indexing",
                    "nodes": None,
                    "edges": None,
                    "files": None,
                    "languages": None,
                    "last_indexed": last_indexed,
                    "repo": repo
                }))

            try:
                import subprocess
                import sys
                proc = subprocess.run(
                    [sys.executable, "-m", "puppetmaster", "codegraph", "status", "--json"],
                    cwd=repo,
                    capture_output=True,
                    text=True,
                    timeout=5
                )
                if proc.returncode == 0:
                    data = json.loads(proc.stdout)
                    initialized = data.get("initialized", False)
                    status_val = "ready" if initialized else "unsupported"
                    
                    last_indexed = None
                    try:
                        import puppetmaster.codegraph as cg
                        mtime = cg.codegraph_index_mtime(repo)
                        if mtime:
                            import datetime
                            last_indexed = datetime.datetime.fromtimestamp(mtime).isoformat()
                    except Exception:
                        pass

                    return self._send(200, json.dumps({
                        "indexed": initialized,
                        "status": status_val,
                        "nodes": data.get("nodeCount"),
                        "edges": data.get("edgeCount"),
                        "files": data.get("fileCount"),
                        "languages": data.get("languages"),
                        "last_indexed": last_indexed,
                        "repo": repo
                    }))
                else:
                    return self._send(200, json.dumps({
                        "indexed": False,
                        "status": "unsupported",
                        "nodes": None,
                        "edges": None,
                        "files": None,
                        "languages": None,
                        "last_indexed": None,
                        "repo": repo
                    }))
            except Exception:
                return self._send(200, json.dumps({
                    "indexed": False,
                    "status": "unsupported",
                    "nodes": None,
                    "edges": None,
                    "files": None,
                    "languages": None,
                    "last_indexed": None,
                    "repo": repo
                }))
        if u.path == "/api/config":
            return self._send(200, json.dumps({
                "driver": _cfg.driver, "reach": _cfg.reach,
                "budget": _cfg.budget, "state_dir": _session.state_dir,
                "models": _available_pilots(), "repo": _cfg.repo,
                "preflight": _session.preflight()}))
        if u.path == "/api/wiki/config":
            if self._guard():
                return
            qtok = parse_qs(u.query).get("token", [""])[0]
            if qtok != _TOKEN and self.headers.get("X-Harness-Token", "") != _TOKEN:
                return self._send(403, json.dumps({"error": "missing or bad token"}))
            return self._send(200, json.dumps(get_wiki_config()))
        if u.path == "/api/wiki/graph":
            if self._guard():
                return
            qtok = parse_qs(u.query).get("token", [""])[0]
            if qtok != _TOKEN and self.headers.get("X-Harness-Token", "") != _TOKEN:
                return self._send(403, json.dumps({"error": "missing or bad token"}))
            
            # WikiClient auto-detects the gated owner surface (WIKI_API_BASE +
            # WIKI_OWNER_TOKEN, same as the portable-llm-wiki MCP) or the public
            # HARNESS_WIKI_URL. config.wiki_url overrides base_url when set.
            from .wiki import WikiClient
            try:
                client = WikiClient(base_url=_cfg.wiki_url or "", timeout=8)
            except Exception as e:
                client = None
                _client_err = str(e)
            if client is None or not client.base_url:
                return self._send(200, json.dumps({
                    "configured": False,
                    "status": "not_configured",
                    "nodes": [],
                    "edges": [],
                    "base_url": ""
                }))
            try:
                res = client.graph()
            except Exception as e:
                res = {"error": f"Unexpected error: {str(e)}", "nodes": [], "edges": []}
            if res.get("error"):
                return self._send(200, json.dumps({
                    "configured": True,
                    "status": "error",
                    "nodes": [],
                    "edges": [],
                    "error": res["error"],
                    "base_url": client.base_url
                }))
            return self._send(200, json.dumps({
                "configured": True,
                "status": "ok",
                "nodes": res.get("nodes") or [],
                "edges": res.get("edges") or [],
                "base_url": client.base_url
            }))
        if u.path == "/api/settings":
            return self._send(200, json.dumps(_get_settings_dict()))
        if u.path == "/api/platform":
            return self._send(200, json.dumps(_get_platform_adapters()))
        if u.path == "/api/jobs":
            return self._send(200, json.dumps(_session.state().list_jobs()))
        if u.path == "/api/usage":
            if self._guard():
                return
            qtok = parse_qs(u.query).get("token", [""])[0]
            if qtok != _TOKEN and self.headers.get("X-Harness-Token", "") != _TOKEN:
                return self._send(403, json.dumps({"error": "missing or bad token"}))
            price_in = 0.5
            price_out = 2.0
            base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            catalog_path = os.path.join(base_dir, "pmharness", "catalog.json")
            if os.path.exists(catalog_path):
                try:
                    with open(catalog_path, "r", encoding="utf-8") as f:
                        catalog_data = json.load(f)
                        for m in catalog_data.get("models", []):
                            if m.get("name") == _cfg.driver or _cfg.driver in m.get("name", "") or m.get("name", "") in _cfg.driver:
                                price_in = m.get("price_in", price_in)
                                price_out = m.get("price_out", price_out)
                                break
                except Exception:
                    pass
            tokens_used = getattr(_pilot, "_tokens_used", 0)
            est_session_cost = (tokens_used / 1.0e6) * price_out
            jobs_list = []
            try:
                jobs = _session.state().list_jobs()
                for j in jobs:
                    jid = j.get("id")
                    if jid:
                        try:
                            from puppetmaster.models import ArtifactType
                            from puppetmaster.usage import aggregate_token_usage
                            store = _session.state().store
                            artifacts = store.list_artifacts(jid)
                            routing = []
                            seen_router_tasks = set()
                            total = 0.0
                            for artifact in artifacts:
                                if artifact.type == ArtifactType.ROUTING and artifact.created_by == "router":
                                    task_id = artifact.task_id
                                    if task_id:
                                        if task_id in seen_router_tasks:
                                            continue
                                        seen_router_tasks.add(task_id)
                                    routing.append(artifact)
                            for artifact in routing:
                                payload = artifact.payload or {}
                                cost = float(payload.get("estimated_cost_usd") or 0.0)
                                total += cost
                            tokens = 0
                            try:
                                token_usage_dict = aggregate_token_usage(artifacts)
                                tokens = token_usage_dict.get("total_tokens", 0)
                            except Exception:
                                pass
                            jobs_list.append({
                                "job_id": jid,
                                "tokens": tokens,
                                "est_cost_usd": round(total, 6)
                            })
                        except Exception:
                            pass
            except Exception:
                pass
            response_data = {
                "session": {
                    "tokens_used": tokens_used,
                    "est_cost_usd": round(est_session_cost, 6),
                    "driver": _cfg.driver,
                    "price_in": price_in,
                    "price_out": price_out
                },
                "jobs": jobs_list
            }
            return self._send(200, json.dumps(response_data))
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
            return self._stream_chat(q.get("message", [""])[0], imgs)
        if u.path == "/api/terminal/stream":
            q = parse_qs(u.query)
            return self._stream_terminal(q.get("id", [""])[0])
        if u.path == "/api/pilot":
            q = parse_qs(u.query)
            return self._swap_pilot(q.get("model", [""])[0])
        if u.path == "/api/workspaces":
            return self._send(200, json.dumps(_ws.list_workspaces(_cfg.repo)))
        if u.path == "/api/worktrees":
            from . import worktrees as _wt
            return self._send(200, json.dumps({
                "worktrees": _wt.list_worktrees(_cfg.repo),
                "max": _wt.get_max_worktrees()
            }))
        if u.path == "/api/hooks":
            from . import hooks as _hk
            return self._send(200, json.dumps({
                "hooks": _hk.get_hooks(),
                "events": _hk.ALLOWED_EVENTS
            }))
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
        
        if _sessions.active and prompt:
            from .sessions import derive_title
            _sessions.set_title_if_default(_sessions.active, derive_title(prompt))

        pre = _session.preflight()
        if pre:
            self.wfile.write(f"data: {json.dumps({'kind':'error','turn':0,'data':{'error':pre}})}\n\n".encode())
            self.wfile.write(b"data: {\"kind\": \"done\"}\n\n")
            self.wfile.flush()
            return

        from .hooks import run_hooks
        ctx = {"session_id": _sessions.active or "", "prompt": prompt}
        run_hooks("preRun", ctx)
        try:
            for ev in _session.run(prompt, images=images or None):
                payload = json.dumps({"kind": ev.kind, "turn": ev.turn, "data": ev.data})
                self.wfile.write(f"data: {payload}\n\n".encode())
                self.wfile.flush()
            self.wfile.write(b"data: {\"kind\": \"done\"}\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            run_hooks("postRun", ctx)

    def _stream_auto(self, objective: str):
        """Stream the fully-auto loop (governor-bounded) over SSE."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self._cors()
        self.end_headers()
        
        if _sessions.active and objective:
            from .sessions import derive_title
            _sessions.set_title_if_default(_sessions.active, derive_title(objective))

        from .hooks import run_hooks
        ctx = {"session_id": _sessions.active or "", "objective": objective}
        run_hooks("preRun", ctx)
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
            run_hooks("postRun", ctx)
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

    def _stream_terminal(self, sid: str):
        """Stream PTY output over SSE. Client sends keystrokes via POST /api/terminal/write."""
        sess = _pty.get(sid)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self._cors()
        self.end_headers()
        if not sess:
            try:
                self.wfile.write(b"data: {\"kind\": \"exit\"}\n\n")
                self.wfile.flush()
            except Exception:
                pass
            return
        offset = 0
        try:
            while sess.alive():
                data, offset = sess.read_since(offset)
                if data:
                    import base64 as _b64
                    payload = json.dumps({"kind": "data", "b64": _b64.b64encode(data).decode("ascii")})
                    self.wfile.write(f"data: {payload}\n\n".encode())
                    self.wfile.flush()
                else:
                    time.sleep(0.05)
            # flush any final bytes after exit
            data, offset = sess.read_since(offset)
            if data:
                import base64 as _b64
                payload = json.dumps({"kind": "data", "b64": _b64.b64encode(data).decode("ascii")})
                self.wfile.write(f"data: {payload}\n\n".encode())
            self.wfile.write(b"data: {\"kind\": \"exit\"}\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _stream_chat(self, message: str, images=None):
        """Stream the conversational PILOT loop: prose messages + collapsible
        action cards (run_swarm) + assistant_done."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        
        if _sessions.active and message:
            from .sessions import derive_title
            _sessions.set_title_if_default(_sessions.active, derive_title(message))
 
        pre = _pilot_preflight()
        if pre:
            self.wfile.write(f"data: {json.dumps({'kind':'error','data':{'error':pre}})}\n\n".encode())
            self.wfile.write(b"data: {\"kind\": \"done\"}\n\n")
            self.wfile.flush()
            return
 
        from .hooks import run_hooks
        ctx = {"session_id": _sessions.active or "", "message": message}
        run_hooks("preRun", ctx)
        try:
            for ev in _pilot.send(message, images=images or None):
                payload = json.dumps({"kind": ev.kind, "data": ev.data})
                self.wfile.write(f"data: {payload}\n\n".encode())
                self.wfile.flush()
            self.wfile.write(b"data: {\"kind\": \"done\"}\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            run_hooks("postRun", ctx)
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
