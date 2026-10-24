"""End to end: a real backend with an encrypted session writes no plaintext.

Each scenario step sends a unique canary through the real backend and a fake
OpenAI-compatible model. After the run, no file under the fake HOME or TMPDIR
may hold a canary. The plain run proves that the search finds canaries.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent / "fixtures"))
import fake_openai_model  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
TOKEN = "t" * 32


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Backend:
    def __init__(self, root: Path, model_port: int, master: bytes = b"", seal_line: bool = False):
        self.root = root
        self.port = _free_port()
        home = root / "home"
        self.state = home / ".pmharness" / "state"
        env = {k: v for k, v in os.environ.items()
               if not k.endswith("_API_KEY") and not k.startswith(("HARNESS_", "PUPPETMASTER_", "PMHARNESS_"))}
        env.update(
            HOME=str(home), USERPROFILE=str(home), HARNESS_TOKEN=TOKEN, HARNESS_STATE_DIR=str(self.state),
            HARNESS_REPO=str(root / "proj"), HARNESS_DRIVER="zai:glm-4.7-flash", ZAI_API_KEY="fake-key",
            ZAI_BASE_URL=f"http://127.0.0.1:{model_port}", PYTHONUNBUFFERED="1",
            TMPDIR=str(root / "tmp"), TEMP=str(root / "tmp"), TMP=str(root / "tmp"),
            HARNESS_MAX_CONTEXT_TOKENS="8000",
        )
        if seal_line:
            env["HARNESS_SEAL_STDIN"] = "1"
        self.log = open(root / "backend.log", "ab")
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "harness.cli", "gui", "--port", str(self.port)],
            cwd=str(REPO), env=env, stdin=subprocess.PIPE if seal_line else subprocess.DEVNULL,
            stdout=self.log, stderr=self.log,
        )
        if seal_line:
            line = {"master_key": base64.b64encode(master).decode()} if master else {}
            self.proc.stdin.write(json.dumps(line).encode() + b"\n")
            self.proc.stdin.close()
        deadline = time.time() + 120
        while time.time() < deadline:
            try:
                self.get("/api/sessions", timeout=3)
                return
            except Exception:
                if self.proc.poll() is not None:
                    break
                time.sleep(0.5)
        code = self.proc.poll()
        self.stop()
        raise AssertionError(f"backend did not start (exit {code}):\n{self.log_tail()}")

    def log_tail(self) -> str:
        if not self.log.closed:
            self.log.flush()
        return (self.root / "backend.log").read_text(errors="replace")[-6000:]

    def _request(self, path, data=None, timeout=60):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=None if data is None else json.dumps(data).encode(),
            headers={"X-Harness-Token": TOKEN, "Content-Type": "application/json"},
            method="GET" if data is None else "POST")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8", "replace")

    def get(self, path, timeout=60):
        return self._request(path, timeout=timeout)

    def post(self, path, data):
        return self._request(path, data)

    def status(self, path):
        try:
            return 200, self.get(path)
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")

    def chat(self, message):
        q = urllib.parse.quote(message)
        for _ in range(60):
            try:
                req = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/chat?message={q}",
                                             headers={"X-Harness-Token": TOKEN})
                out = []
                with urllib.request.urlopen(req, timeout=180) as r:
                    for raw in r:
                        out.append(raw.decode("utf-8", "replace"))
                        if '"assistant_done"' in out[-1] or '"turn_terminal"' in out[-1]:
                            break
                return "".join(out)
            except urllib.error.HTTPError as e:
                if e.code not in (409, 503):
                    raise
                time.sleep(1)
            except OSError as e:
                raise AssertionError(f"chat failed ({e!r}); stream so far:\n{''.join(out)[-3000:]}\n"
                                     f"backend log:\n{self.log_tail()}") from e
        raise AssertionError("chat never accepted")

    def stop(self):
        if self.log.closed:
            return
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(30)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(10)
        self.log.close()


@pytest.fixture
def model():
    server = fake_openai_model.serve(0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_address[1]
    server.shutdown()


def _root(tmp_path: Path) -> Path:
    for sub in ("home", "proj", "tmp"):
        (tmp_path / sub).mkdir()
    subprocess.run(["git", "init", "-q", str(tmp_path / "proj")], check=True)
    return tmp_path


def _configure_seal(state: Path) -> bytes:
    master = secrets.token_bytes(32)
    (state / "seal").mkdir(parents=True)
    kid = hmac.new(master, b"marionette-seal-key-id", hashlib.sha256).digest()[:8].hex()
    (state / "seal" / "config.json").write_text(json.dumps({"v": 1, "enabled": True, "key_id": kid}))
    return master


def _scenario(backend: Backend, tag: str) -> str:
    def c(step):
        return f"CNRY{step}x{tag}"

    backend.post("/api/workspace/open", {"path": str(backend.root / "proj")})
    sid = json.loads(backend.post("/api/sessions/create", {}))["id"]
    assert "ECHO" in backend.chat(f"remember {c('chat')}")
    command = f"python -c \"print('{c('cmd')} ' * 3000)\""
    assert "TOOLRESULT" in backend.chat("TOOL run_command " + json.dumps({"command": command}))
    backend.chat("TOOL todo " + json.dumps({"op": "init", "list": [{"phase": "p", "items": [c("todo")]}]}))
    backend.chat("TOOL store_scratch " + json.dumps({"key": "k", "value": c("scratch")}))
    for _ in range(4):
        backend.chat(f"long {c('long')} " + "lorem ipsum dolor sit amet " * 300)
    backend.post("/api/sessions/rename", {"session": sid, "title": c("title")})
    found = backend.get("/api/sessions/search?q=" + urllib.parse.quote(c("chat")))
    assert c("chat") in found
    return sid


def _canary_files(root: Path, tag: str) -> list:
    needle = f"x{tag}".encode()
    hits = []
    for path in root.rglob("*"):
        if path.is_file() and not path.is_relative_to(root / "proj"):
            try:
                if needle in path.read_bytes():
                    hits.append(str(path.relative_to(root)))
            except OSError:
                pass
    return sorted(hits)


def test_plain_session_writes_canaries(tmp_path, model):
    """Power check: the same scenario in a plain session leaves plaintext."""
    root = _root(tmp_path)
    tag = secrets.token_hex(4)
    backend = Backend(root, model)
    try:
        _scenario(backend, tag)
    finally:
        backend.stop()
    hits = _canary_files(root, tag)
    assert any(h.endswith(".json") and "transcripts" in h for h in hits), hits


def test_sealed_session_writes_no_plaintext_and_fails_closed(tmp_path, model):
    root = _root(tmp_path)
    tag = secrets.token_hex(4)
    master = _configure_seal(root / "home" / ".pmharness" / "state")
    backend = Backend(root, model, master, seal_line=True)
    try:
        sid = _scenario(backend, tag)
    finally:
        backend.stop()
    assert _canary_files(root, tag) == []
    transcript = backend.state / "transcripts" / f"{sid}.json"
    before = transcript.read_bytes()
    assert before.startswith(b"MNSL")

    backend = Backend(root, model, master, seal_line=True)
    try:
        code, body = backend.status(f"/api/sessions/transcript?session={sid}")
        assert code == 200 and f"CNRYchatx{tag}" in body
    finally:
        backend.stop()

    backend = Backend(root, model, b"", seal_line=True)
    try:
        code, body = backend.status(f"/api/sessions/transcript?session={sid}")
        assert code == 423 and "sealed_locked" in body
        assert f"x{tag}" not in backend.get("/api/sessions")
        code, body = backend.status("/api/seal/status")
        assert json.loads(body) == {"configured": True, "enabled": True, "unlocked": False}
    finally:
        backend.stop()
    assert transcript.read_bytes() == before
    assert _canary_files(root, tag) == []
