"""Hermetic download, extract, and process-ownership tests for local models."""
from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import subprocess
import tarfile
import threading
import time
import urllib.error
import urllib.request
import zipfile
from types import SimpleNamespace

import pytest

from harness.local_model_manager import (
    AssetRedirectHandler,
    DOWNLOAD_PROGRESS_BYTES,
    DOWNLOAD_PROGRESS_INTERVAL,
    EventLog,
    LocalModelError,
    LocalModelManager,
    ProbeRedirectHandler,
    _CREATE_NEW_PROCESS_GROUP,
    _CREATE_NO_WINDOW,
    _DETACHED_PROCESS,
    _SIGKILL,
    _SIGTERM,
    _pid_alive,
    assert_probe_hop_safe,
    download_host_allowed,
    extract_archive,
    is_safe_archive_member,
    parse_content_range,
    probe_urlopen,
    process_matches_identity,
    read_process_command,
    read_process_start_key,
    sha256_file,
    spawn_popen_kwargs,
    stop_process_tree,
    validate_download_url,
    zip_is_symlink,
)


def _tiny_catalog(tmp_path, payload=b"runtime-bytes"):
    digest = hashlib.sha256(payload).hexdigest()
    model = b"gguf-bytes"
    model_digest = hashlib.sha256(model).hexdigest()
    return {
        "version": 1,
        "runtime": {
            "id": "llama.cpp",
            "release": "test",
            "binary": "llama-server",
            "assets": {
                "macos-arm64": {
                    "filename": "runtime.tar.gz",
                    "url": "http://fixture.test/runtime.tar.gz",
                    "sha256": digest,
                    "size": len(payload),
                    "archive": "tar.gz",
                },
                "macos-x64": {
                    "filename": "runtime.tar.gz",
                    "url": "http://fixture.test/runtime.tar.gz",
                    "sha256": digest,
                    "size": len(payload),
                    "archive": "tar.gz",
                },
                "linux-x64": {
                    "filename": "runtime.tar.gz",
                    "url": "http://fixture.test/runtime.tar.gz",
                    "sha256": digest,
                    "size": len(payload),
                    "archive": "tar.gz",
                },
                "linux-arm64": {
                    "filename": "runtime.tar.gz",
                    "url": "http://fixture.test/runtime.tar.gz",
                    "sha256": digest,
                    "size": len(payload),
                    "archive": "tar.gz",
                },
                "windows-x64": {
                    "filename": "runtime.tar.gz",
                    "url": "http://fixture.test/runtime.tar.gz",
                    "sha256": digest,
                    "size": len(payload),
                    "archive": "tar.gz",
                },
                "windows-arm64": {
                    "filename": "runtime.tar.gz",
                    "url": "http://fixture.test/runtime.tar.gz",
                    "sha256": digest,
                    "size": len(payload),
                    "archive": "tar.gz",
                },
            },
        },
        "models": [{
            "id": "qwen-test",
            "name": "Qwen test",
            "filename": "model.gguf",
            "url": "http://fixture.test/model.gguf",
            "revision": "abc",
            "sha256": model_digest,
            "size": len(model),
            "context_length": 2048,
            "min_ram_gb": 0,
            "recommended_ram_gb": 1,
            "min_disk_bytes": 1,
        }],
    }, payload, model


class _RangeBody:
    def __init__(self, data, start=0, status=200, headers=None, total=None):
        self._buf = io.BytesIO(data[start:] if status == 206 else data)
        self.status = status
        hdrs = dict(headers or {})
        if status == 206:
            end = (total or len(data)) - 1
            hdrs.setdefault(
                "Content-Range",
                "bytes %s-%s/%s" % (start, end, total if total is not None else len(data)),
            )
        self.headers = hdrs

    def read(self, n=-1):
        return self._buf.read(n)

    def close(self):
        return None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_safe_archive_member_rejects_traversal():
    assert is_safe_archive_member("bin/llama-server")
    assert not is_safe_archive_member("../etc/passwd")
    assert not is_safe_archive_member("/etc/passwd")
    assert not is_safe_archive_member("foo/../../x")


def test_extract_archive_rejects_zip_slip(tmp_path):
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("../escape.txt", "nope")
    with pytest.raises(LocalModelError) as exc:
        extract_archive(str(archive), str(tmp_path / "out"))
    assert exc.value.code == "unsafe_archive"


def test_extract_archive_promotes_safe_tar(tmp_path):
    archive = tmp_path / "ok.tar.gz"
    payload = tmp_path / "payload"
    payload.mkdir()
    (payload / "llama-server").write_text("bin", encoding="utf-8")
    with tarfile.open(archive, "w:gz") as tf:
        tf.add(payload / "llama-server", arcname="bin/llama-server")
    dest = tmp_path / "runtime"
    extract_archive(str(archive), str(dest))
    assert (dest / "bin" / "llama-server").is_file()


def test_download_resume_cancel_and_hash(tmp_path, monkeypatch):
    catalog, runtime_bytes, _model = _tiny_catalog(tmp_path)
    root = tmp_path / "lm"
    calls = {"n": 0}

    def urlopen(req, timeout=None):
        calls["n"] += 1
        start = 0
        rng = req.headers.get("Range") or req.get_header("Range")
        if rng and rng.startswith("bytes="):
            start = int(rng.split("=")[1].split("-")[0] or 0)
            return _RangeBody(runtime_bytes, start, status=206, total=len(runtime_bytes))
        return _RangeBody(runtime_bytes, 0, status=200)

    mgr = LocalModelManager(root=str(root), catalog=catalog, urlopen=urlopen)
    asset = catalog["runtime"]["assets"]["linux-x64"]
    # First pass: write part then cancel mid-way by pre-seeding part.
    part = mgr._part_path(asset["filename"])
    os.makedirs(os.path.dirname(part), exist_ok=True)
    with open(part, "wb") as handle:
        handle.write(runtime_bytes[:4])
    path = mgr.download_asset(asset, target="runtime")
    assert os.path.isfile(path)
    assert sha256_file(path) == asset["sha256"]
    assert calls["n"] == 1

    bad = dict(asset, sha256="0" * 64)
    with pytest.raises(LocalModelError) as exc:
        mgr.download_asset(bad, target="runtime")
    assert exc.value.code == "hash_mismatch"


def test_download_cancel_does_not_promote(tmp_path):
    catalog, runtime_bytes, _model = _tiny_catalog(tmp_path)
    mgr = LocalModelManager(root=str(tmp_path / "lm"), catalog=catalog)
    asset = catalog["runtime"]["assets"]["linux-x64"]

    class _Chunked:
        def __init__(self):
            self.sent = False

        def read(self, n=-1):
            if not self.sent:
                self.sent = True
                mgr.cancel("runtime")
                return runtime_bytes[:2]
            return runtime_bytes[2:]

        def close(self):
            return None

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    mgr.urlopen = lambda req, timeout=None: _Chunked()
    with pytest.raises(LocalModelError) as exc:
        mgr.download_asset(asset, target="runtime")
    assert exc.value.code == "cancelled"
    assert not os.path.exists(mgr._final_path(asset["filename"]))


def test_stale_pid_never_kills_unrelated(tmp_path, monkeypatch):
    catalog, _, _ = _tiny_catalog(tmp_path)
    killed = []
    monkeypatch.setattr("harness.local_model_manager.os.kill", lambda pid, sig: killed.append((pid, sig)))
    monkeypatch.setattr("harness.local_model_manager._pid_alive", lambda pid: True)
    monkeypatch.setattr(
        "harness.local_model_manager.read_process_command",
        lambda pid: "/usr/bin/unrelated --port 9",
    )
    mgr = LocalModelManager(root=str(tmp_path / "lm"), catalog=catalog)
    state = mgr._state()
    state["managed"]["process"] = {
        "pid": 4242,
        "port": 9,
        "host": "127.0.0.1",
        "exe": "/tmp/llama-server",
        "model_path": "/tmp/model.gguf",
        "alias": "marionette-deadbeef",
        "nonce": "deadbeef",
        "healthy": True,
    }
    mgr._save(state)
    mgr.reconcile_process()
    assert mgr._state()["managed"]["process"] is None
    assert killed == []


def test_process_matches_identity_requires_alias():
    identity = {
        "alias": "marionette-abc",
        "nonce": "abc",
        "exe": "llama-server",
        "model_path": "/tmp/qwen.gguf",
    }
    assert process_matches_identity(os.getpid(), identity) is False


def test_start_uses_injected_popen_and_ready(tmp_path, monkeypatch):
    catalog, _, model_bytes = _tiny_catalog(tmp_path)
    root = tmp_path / "lm"
    mgr = LocalModelManager(
        root=str(root),
        catalog=catalog,
        urlopen=lambda *a, **k: (_ for _ in ()).throw(AssertionError("no download")),
        sleeper=lambda _s: None,
        clock=lambda: 1.0,
        ready_timeout=1.0,
    )
    runtime = root / "runtime" / "test"
    runtime.mkdir(parents=True)
    exe = runtime / ("llama-server.exe" if os.name == "nt" else "llama-server")
    exe.write_text("x", encoding="utf-8")
    models = root / "models"
    models.mkdir()
    model_path = models / "model.gguf"
    model_path.write_bytes(model_bytes)
    mgr._set_component("runtime", status="ready", path=str(exe))
    mgr._set_component("model", status="ready", path=str(model_path), id="qwen-test")

    spawned = {}

    class _Proc:
        pid = 321

        def wait(self, timeout=None):
            return 0

        def kill(self):
            return None

    def popen(argv, **kwargs):
        spawned["argv"] = argv
        spawned["kwargs"] = kwargs
        return _Proc()

    mgr.popen = popen
    mgr._probe_health = lambda url, required_alias="": True
    snap = mgr.start()
    assert "--jinja" in spawned["argv"]
    assert "--host" in spawned["argv"]
    assert spawned["argv"][spawned["argv"].index("--host") + 1] == "127.0.0.1"
    assert "--alias" in spawned["argv"]
    assert snap["managed"]["process"]["healthy"] is True
    assert snap["managed"]["process"]["alias"]
    assert snap["managed"]["process"]["nonce"]
    if os.name == "nt":
        flags = spawned["kwargs"].get("creationflags") or 0
        assert flags & _CREATE_NEW_PROCESS_GROUP
        assert flags & _CREATE_NO_WINDOW
        assert not (flags & _DETACHED_PROCESS)
        assert "start_new_session" not in spawned["kwargs"]
    else:
        assert spawned["kwargs"].get("start_new_session") is True


def test_probe_uses_injected_transport(tmp_path):
    catalog, _, _ = _tiny_catalog(tmp_path)

    def transport(url, **kwargs):
        return {
            "payload": {"data": [{"id": "llama3", "max_model_len": 4096}]},
            "headers": {"Server": "ollama"},
            "status": 200,
        }

    mgr = LocalModelManager(
        root=str(tmp_path / "lm"),
        catalog=catalog,
        probe_transport=transport,
    )
    result = mgr.probe("http://127.0.0.1:11434")
    assert result["vendor"] == "ollama"
    assert result["models"] == ["llama3"]
    assert result["context_length"] == 4096
    assert "api_key" not in result


def test_download_host_allowlist():
    assert download_host_allowed("github.com")
    assert download_host_allowed("objects.githubusercontent.com")
    assert download_host_allowed("release-assets.githubusercontent.com")
    assert download_host_allowed("huggingface.co")
    assert download_host_allowed("cas-bridge.xethub.hf.co")
    assert download_host_allowed("us.aws.cdn.hf.co")
    assert not download_host_allowed("evil.example")
    assert not download_host_allowed("169.254.169.254")


def test_validate_download_url_https_allowlist(monkeypatch):
    monkeypatch.setattr(
        "harness.local_model_manager.is_safe_url_pinned",
        lambda url, **k: (True, "", "1.2.3.4"),
    )
    validate_download_url("https://github.com/ggml-org/llama.cpp/releases/download/b10442/x.tgz")
    with pytest.raises(LocalModelError) as exc:
        validate_download_url("http://github.com/ggml-org/llama.cpp/x")
    assert exc.value.code == "unsafe_url"
    with pytest.raises(LocalModelError):
        validate_download_url("https://evil.example/x")


def test_validate_download_rejects_private_ip(monkeypatch):
    monkeypatch.setattr(
        "harness.local_model_manager.is_safe_url_pinned",
        lambda url, **k: (False, "blocked private or reserved IP address 10.0.0.5", None),
    )
    with pytest.raises(LocalModelError) as exc:
        validate_download_url("https://huggingface.co/Qwen/x")
    assert exc.value.code == "unsafe_url"


def test_asset_redirect_max_and_https():
    assert AssetRedirectHandler.max_redirections == 5
    handler = AssetRedirectHandler()
    req = urllib.request.Request("https://github.com/ggml-org/llama.cpp/x")
    with pytest.raises(urllib.error.HTTPError):
        handler.redirect_request(req, None, 302, "Found", {}, "http://github.com/x")


def test_download_200_on_range_truncates(tmp_path):
    catalog, runtime_bytes, _model = _tiny_catalog(tmp_path)
    mgr = LocalModelManager(root=str(tmp_path / "lm"), catalog=catalog)
    asset = catalog["runtime"]["assets"]["linux-x64"]
    part = mgr._part_path(asset["filename"])
    os.makedirs(os.path.dirname(part), exist_ok=True)
    with open(part, "wb") as handle:
        handle.write(b"XXXX")

    def urlopen(req, timeout=None):
        assert req.headers.get("Range") or req.get_header("Range")
        return _RangeBody(runtime_bytes, status=200)

    mgr.urlopen = urlopen
    path = mgr.download_asset(asset, target="runtime")
    assert sha256_file(path) == asset["sha256"]
    assert os.path.getsize(path) == len(runtime_bytes)


def test_download_range_mismatch_rejects_before_write(tmp_path):
    catalog, runtime_bytes, _model = _tiny_catalog(tmp_path)
    mgr = LocalModelManager(root=str(tmp_path / "lm"), catalog=catalog)
    asset = catalog["runtime"]["assets"]["linux-x64"]
    part = mgr._part_path(asset["filename"])
    os.makedirs(os.path.dirname(part), exist_ok=True)
    with open(part, "wb") as handle:
        handle.write(runtime_bytes[:4])

    def urlopen(req, timeout=None):
        return _RangeBody(
            runtime_bytes, start=0, status=206,
            headers={"Content-Range": "bytes 0-10/999"},
        )

    mgr.urlopen = urlopen
    with pytest.raises(LocalModelError) as exc:
        mgr.download_asset(asset, target="runtime")
    assert exc.value.code == "download"
    assert os.path.getsize(part) == 4
    assert not os.path.exists(mgr._final_path(asset["filename"]))


def test_download_oversized_does_not_promote(tmp_path):
    catalog, runtime_bytes, _model = _tiny_catalog(tmp_path)
    mgr = LocalModelManager(root=str(tmp_path / "lm"), catalog=catalog)
    asset = dict(catalog["runtime"]["assets"]["linux-x64"], size=4)

    def urlopen(req, timeout=None):
        return _RangeBody(runtime_bytes, status=200)

    mgr.urlopen = urlopen
    with pytest.raises(LocalModelError) as exc:
        mgr.download_asset(asset, target="runtime")
    assert exc.value.code == "download"
    assert not os.path.exists(mgr._final_path(asset["filename"]))


def test_download_partial_does_not_promote(tmp_path):
    catalog, runtime_bytes, _model = _tiny_catalog(tmp_path)
    mgr = LocalModelManager(root=str(tmp_path / "lm"), catalog=catalog)
    asset = catalog["runtime"]["assets"]["linux-x64"]

    def urlopen(req, timeout=None):
        return _RangeBody(runtime_bytes[:4], status=200)

    mgr.urlopen = urlopen
    with pytest.raises(LocalModelError) as exc:
        mgr.download_asset(asset, target="runtime")
    assert exc.value.code == "download"
    assert not os.path.exists(mgr._final_path(asset["filename"]))
    part = mgr._part_path(asset["filename"])
    assert os.path.exists(part)
    assert 0 < os.path.getsize(part) < asset["size"]


def test_parse_content_range_requires_total():
    assert parse_content_range("bytes 10-20/100") == (10, 20, 100)
    assert parse_content_range("bytes 10-20/*") == (10, 20, None)
    assert parse_content_range("not-a-range") is None


def test_extract_rejects_tar_symlink(tmp_path):
    archive = tmp_path / "link.tar"
    with tarfile.open(archive, "w") as tf:
        info = tarfile.TarInfo("evil")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/passwd"
        tf.addfile(info)
    with pytest.raises(LocalModelError) as exc:
        extract_archive(str(archive), str(tmp_path / "out"))
    assert exc.value.code == "unsafe_archive"
    assert not (tmp_path / "out").exists()


def test_extract_rejects_tar_hardlink_and_fifo(tmp_path):
    archive = tmp_path / "fifo.tar"
    with tarfile.open(archive, "w") as tf:
        info = tarfile.TarInfo("pipe")
        info.type = tarfile.FIFOTYPE
        tf.addfile(info)
    with pytest.raises(LocalModelError) as exc:
        extract_archive(str(archive), str(tmp_path / "out"))
    assert exc.value.code == "unsafe_archive"


def test_extract_rejects_zip_symlink(tmp_path):
    archive = tmp_path / "link.zip"
    info = zipfile.ZipInfo("link")
    info.create_system = 3
    info.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr(info, "/etc/passwd")
    assert zip_is_symlink(info)
    with pytest.raises(LocalModelError) as exc:
        extract_archive(str(archive), str(tmp_path / "out"))
    assert exc.value.code == "unsafe_archive"


def test_windows_spawn_flags(monkeypatch):
    monkeypatch.setattr("harness.local_model_manager._platform_name", lambda: "nt")
    kwargs = spawn_popen_kwargs()
    flags = kwargs.get("creationflags") or 0
    assert flags & _CREATE_NEW_PROCESS_GROUP
    assert flags & _CREATE_NO_WINDOW
    assert not (flags & _DETACHED_PROCESS)
    assert "start_new_session" not in kwargs


def test_posix_spawn_new_session(monkeypatch):
    monkeypatch.setattr("harness.local_model_manager._platform_name", lambda: "posix")
    kwargs = spawn_popen_kwargs()
    assert kwargs.get("start_new_session") is True


def test_posix_tree_stop_term_then_kill(monkeypatch):
    killed = []
    monkeypatch.setattr("harness.local_model_manager._platform_name", lambda: "posix")
    monkeypatch.setattr(os, "getpgid", lambda pid: 9001, raising=False)
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: killed.append((pgid, sig)), raising=False)
    stop_process_tree(4242, proc=None, grace=0, sleeper=lambda _s: None)
    assert killed == [(9001, _SIGTERM), (9001, _SIGKILL)]


def test_windows_tree_stop_taskkill(monkeypatch):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("harness.local_model_manager._platform_name", lambda: "nt")
    monkeypatch.setattr("harness.local_model_manager.subprocess.run", fake_run)
    stop_process_tree(4242, proc=None, grace=0, sleeper=lambda _s: None)
    assert calls[0][:4] == ["taskkill", "/PID", "4242", "/T"]
    assert "/F" not in calls[0]
    assert calls[1] == ["taskkill", "/PID", "4242", "/T", "/F"]


def test_stop_never_signals_unmatched(tmp_path, monkeypatch):
    catalog, _, _ = _tiny_catalog(tmp_path)
    signaled = []
    monkeypatch.setattr(
        "harness.local_model_manager.stop_process_tree",
        lambda *a, **k: signaled.append(a),
    )
    monkeypatch.setattr("harness.local_model_manager._pid_alive", lambda pid: True)
    monkeypatch.setattr(
        "harness.local_model_manager.read_process_command",
        lambda pid: "/usr/bin/unrelated",
    )
    mgr = LocalModelManager(root=str(tmp_path / "lm"), catalog=catalog)
    state = mgr._state()
    state["managed"]["process"] = {
        "pid": 4242, "port": 9, "host": "127.0.0.1",
        "exe": "/tmp/llama-server", "model_path": "/tmp/model.gguf",
        "alias": "marionette-deadbeef", "nonce": "deadbeef",
    }
    mgr._save(state)
    mgr.stop()
    assert signaled == []
    assert mgr._state()["managed"]["process"] is None


def test_readiness_requires_alias(tmp_path, monkeypatch):
    catalog, _, model_bytes = _tiny_catalog(tmp_path)
    root = tmp_path / "lm"
    ticks = {"n": 0}
    stopped = []
    monkeypatch.setattr(
        "harness.local_model_manager.stop_process_tree",
        lambda pid, *args, **kwargs: stopped.append(pid),
    )

    def clock():
        ticks["n"] += 1
        return float(ticks["n"])

    mgr = LocalModelManager(
        root=str(root), catalog=catalog, sleeper=lambda _s: None,
        clock=clock, ready_timeout=2.0,
    )
    runtime = root / "runtime" / "test"
    runtime.mkdir(parents=True)
    exe = runtime / "llama-server"
    exe.write_text("x", encoding="utf-8")
    models = root / "models"
    models.mkdir()
    model_path = models / "model.gguf"
    model_path.write_bytes(model_bytes)
    mgr._set_component("runtime", status="ready", path=str(exe))
    mgr._set_component("model", status="ready", path=str(model_path), id="qwen-test")

    class _Proc:
        pid = 77

        def poll(self):
            return None

        def wait(self, timeout=None):
            return 0

        def kill(self):
            return None

    mgr.popen = lambda *a, **k: _Proc()
    mgr._http_json = lambda *a, **k: {"payload": {"data": [{"id": "someone-else"}]}}
    with pytest.raises(LocalModelError) as exc:
        mgr.start()
    assert exc.value.code == "not_ready"
    assert stopped == [77]
    assert mgr._state()["managed"]["process"] is None
    assert mgr._procs == {}


def test_readiness_fails_if_child_exits(tmp_path, monkeypatch):
    catalog, _, model_bytes = _tiny_catalog(tmp_path)
    root = tmp_path / "lm"
    stopped = []
    monkeypatch.setattr(
        "harness.local_model_manager.stop_process_tree",
        lambda pid, *args, **kwargs: stopped.append(pid),
    )
    mgr = LocalModelManager(
        root=str(root), catalog=catalog, sleeper=lambda _s: None,
        clock=lambda: 1.0, ready_timeout=1.0,
    )
    runtime = root / "runtime" / "test"
    runtime.mkdir(parents=True)
    exe = runtime / "llama-server"
    exe.write_text("x", encoding="utf-8")
    (root / "models").mkdir()
    model_path = root / "models" / "model.gguf"
    model_path.write_bytes(model_bytes)
    mgr._set_component("runtime", status="ready", path=str(exe))
    mgr._set_component("model", status="ready", path=str(model_path), id="qwen-test")

    class _Dead:
        pid = 88

        def poll(self):
            return 1

        def wait(self, timeout=None):
            return 1

        def kill(self):
            return None

    mgr.popen = lambda *a, **k: _Dead()
    mgr._http_json = lambda *a, **k: {"payload": {"data": [{"id": "marionette-x"}]}}
    with pytest.raises(LocalModelError) as exc:
        mgr.start()
    assert exc.value.code == "not_ready"
    assert stopped == [88]
    assert mgr._procs == {}


def test_shutdown_is_idempotent(tmp_path, monkeypatch):
    catalog, _, _ = _tiny_catalog(tmp_path)
    mgr = LocalModelManager(root=str(tmp_path / "lm"), catalog=catalog)
    mgr.shutdown()
    mgr.shutdown()
    assert mgr._shutdown is True


def test_shutdown_stops_owned_handle_when_ps_fails(tmp_path, monkeypatch):
    catalog, _, _ = _tiny_catalog(tmp_path)
    stopped = []
    monkeypatch.setattr(
        "harness.local_model_manager.stop_process_tree",
        lambda pid, proc=None, **k: stopped.append((pid, proc)),
    )
    monkeypatch.setattr("harness.local_model_manager.read_process_command", lambda pid: "")
    monkeypatch.setattr("harness.local_model_manager._pid_alive", lambda pid: True)
    mgr = LocalModelManager(root=str(tmp_path / "lm"), catalog=catalog)

    class _Proc:
        pid = 4242

        def wait(self, timeout=None):
            return 0

        def kill(self):
            return None

    log = open(os.path.join(str(tmp_path), "llama-shutdown.log"), "wb")
    proc = _Proc()
    mgr._procs[4242] = (proc, log)
    state = mgr._state()
    state["managed"]["process"] = {
        "pid": 4242, "port": 9, "host": "127.0.0.1",
        "exe": "/tmp/llama-server", "model_path": "/tmp/model.gguf",
        "alias": "marionette-deadbeef", "nonce": "deadbeef",
    }
    mgr._save(state)
    mgr.shutdown()
    assert stopped
    assert stopped[0][0] == 4242
    assert log.closed
    assert mgr._state()["managed"]["process"] is None


def test_event_cursor_monotonic_across_restart(tmp_path):
    catalog, _, _ = _tiny_catalog(tmp_path)
    root = str(tmp_path / "lm")
    first = LocalModelManager(root=root, catalog=catalog)
    first._emit("progress", {"n": 1})
    cursor = first.events.cursor
    assert cursor > 0
    second = LocalModelManager(root=root, catalog=catalog)
    assert second.events.cursor >= cursor
    replay = second.events_since(cursor - 1)
    assert replay
    assert replay[0]["kind"] == "snapshot"
    assert replay[0]["data"]["reason"] == "replay_unavailable"
    assert replay[0]["cursor"] >= cursor


def test_event_log_gap_emits_snapshot():
    log = EventLog(cursor=12)
    events = log.since(3)
    assert events[0]["kind"] == "snapshot"
    assert events[0]["cursor"] == 12


def test_probe_redirect_refuses_public():
    handler = ProbeRedirectHandler()
    req = urllib.request.Request("http://127.0.0.1:8080/v1/models")
    with pytest.raises(urllib.error.HTTPError) as exc:
        handler.redirect_request(req, None, 302, "Found", {}, "https://example.com/steal")
    assert "Public" in str(exc.value) or "blocked" in str(exc.value).lower() or exc.value.code == 302


def test_probe_redirect_refuses_metadata():
    handler = ProbeRedirectHandler()
    req = urllib.request.Request("http://127.0.0.1:8080/v1/models")
    with pytest.raises(urllib.error.HTTPError):
        handler.redirect_request(
            req, None, 302, "Found", {}, "http://169.254.169.254/latest/meta-data/",
        )


def test_assert_probe_hop_refuses_public_ip(monkeypatch):
    monkeypatch.setattr(
        "harness.local_model_manager.is_safe_url_pinned",
        lambda url, **k: (True, "", "8.8.8.8"),
    )
    with pytest.raises(LocalModelError) as exc:
        assert_probe_hop_safe("http://127.0.0.1:8080/v1")
    assert exc.value.code == "public"


def test_install_requires_explicit_model_id(tmp_path):
    catalog, _, _ = _tiny_catalog(tmp_path)
    mgr = LocalModelManager(root=str(tmp_path / "lm"), catalog=catalog)
    with pytest.raises(LocalModelError) as exc:
        mgr.install("all", model_id="", background=False)
    assert exc.value.code == "model_id"
    with pytest.raises(LocalModelError) as exc:
        mgr.install("all", model_id="missing", background=False)
    assert exc.value.code == "unknown_model"


def test_runpod_https_save_with_manual_model(tmp_path, monkeypatch):
    catalog, _, _ = _tiny_catalog(tmp_path)
    monkeypatch.setattr(
        "harness.local_model_manager.is_safe_url_pinned",
        lambda url, **k: (True, "", "1.2.3.4"),
    )

    def transport(url, **kwargs):
        raise LocalModelError("Endpoint returned HTTP 404", code="probe", http_status=404)

    monkeypatch.setattr("harness.keys.set_api_key", lambda *a, **k: None)
    mgr = LocalModelManager(
        root=str(tmp_path / "lm"),
        catalog=catalog,
        probe_transport=transport,
    )
    snap = mgr.save_external(
        "https://abc.proxy.runpod.net/v1",
        accept_remote=True,
        model="qwen3-4b",
        name="runpod-box",
        api_key="sk-secret-value",
    )
    blob = json.dumps(snap)
    assert "sk-secret-value" not in blob
    row = snap["externals"][0]
    assert row["selected_model"] == "qwen3-4b"
    assert row["name"] == "runpod-box"
    assert row["vendor"] == "openai-compatible"
    assert row["healthy"] is True
    assert row["remote_accepted"] is True
    assert row.get("api_key") in (None, "", "••••")
    resolved = mgr.resolve_spec("local:%s/qwen3-4b" % row["id"])
    assert resolved["base_url"].startswith("https://")
    assert resolved["secret_reach"].startswith("local-")


def test_runpod_rejected_without_confirmation(tmp_path):
    catalog, _, _ = _tiny_catalog(tmp_path)
    mgr = LocalModelManager(root=str(tmp_path / "lm"), catalog=catalog)
    with pytest.raises(LocalModelError) as exc:
        mgr.save_external(
            "https://abc.proxy.runpod.net/v1",
            accept_remote=False,
            model="qwen3-4b",
        )
    assert exc.value.code == "public"


def test_external_survives_manager_restart(tmp_path, monkeypatch):
    catalog, _, _ = _tiny_catalog(tmp_path)
    monkeypatch.setattr(
        "harness.local_model_manager.is_safe_url_pinned",
        lambda url, **k: (True, "", "1.2.3.4"),
    )
    root = str(tmp_path / "lm")
    first = LocalModelManager(
        root=root,
        catalog=catalog,
        probe_transport=lambda *a, **k: {
            "payload": {"data": []},
            "headers": {},
            "status": 200,
        },
    )
    first.save_external(
        "https://abc.proxy.runpod.net/v1",
        accept_remote=True,
        model="manual-qwen",
        name="kept",
    )
    second = LocalModelManager(root=root, catalog=catalog)
    specs = second.usable_specs()
    assert any(spec.endswith("/manual-qwen") for spec in specs)
    row = second._state()["externals"][0]
    resolved = second.resolve_spec("local:%s/manual-qwen" % row["id"])
    assert resolved["base_url"]
    assert resolved["secret_reach"] == lm_reach(row["id"])


def lm_reach(endpoint_id):
    from harness.local_models import local_secret_reach
    return local_secret_reach(endpoint_id)


def test_probe_redirect_refuses_public_even_when_remote_accepted():
    handler = ProbeRedirectHandler()
    req = urllib.request.Request("http://127.0.0.1:8080/v1/models")
    with pytest.raises(urllib.error.HTTPError):
        handler.redirect_request(req, None, 302, "Found", {}, "https://example.com/steal")


def test_assert_probe_hop_allows_confirmed_public(monkeypatch):
    monkeypatch.setattr(
        "harness.local_model_manager.is_safe_url_pinned",
        lambda url, **k: (True, "", "1.2.3.4"),
    )
    decision = assert_probe_hop_safe(
        "https://abc.proxy.runpod.net/v1",
        accept_remote=True,
    )
    assert decision["ok"] is True
    assert decision["kind"] == "public"


def test_activate_rejects_unhealthy_external(tmp_path):
    catalog, _, _ = _tiny_catalog(tmp_path)
    mgr = LocalModelManager(root=str(tmp_path / "lm"), catalog=catalog)
    state = mgr._state()
    state["externals"] = [{
        "id": "ollama-127-0-0-1-11434",
        "selected_model": "llama3",
        "base_url": "http://127.0.0.1:11434/v1",
        "healthy": False,
    }]
    mgr._save(state)
    with pytest.raises(LocalModelError) as exc:
        mgr.activate("local:ollama-127-0-0-1-11434/llama3")
    assert exc.value.code == "unhealthy"


def test_runpod_wrong_key_is_auth_error(tmp_path, monkeypatch):
    catalog, _, _ = _tiny_catalog(tmp_path)
    monkeypatch.setattr(
        "harness.local_model_manager.is_safe_url_pinned",
        lambda url, **k: (True, "", "1.2.3.4"),
    )

    def transport(url, **kwargs):
        raise LocalModelError("Endpoint returned HTTP 401: unauthorized", code="probe", http_status=401)

    mgr = LocalModelManager(
        root=str(tmp_path / "lm"), catalog=catalog, probe_transport=transport,
    )
    with pytest.raises(LocalModelError) as exc:
        mgr.probe(
            "https://api.runpod.ai/v2/xxx/openai/v1",
            api_key="wrong-key",
            accept_remote=True,
        )
    assert exc.value.code == "auth"
    assert exc.value.http_status == 401


def test_runpod_missing_key_status_is_auth_error(tmp_path, monkeypatch):
    catalog, _, _ = _tiny_catalog(tmp_path)
    monkeypatch.setattr(
        "harness.local_model_manager.is_safe_url_pinned",
        lambda url, **k: (True, "", "1.2.3.4"),
    )

    def transport(url, **kwargs):
        return {"payload": {"error": "unauthorized"}, "headers": {}, "status": 403}

    mgr = LocalModelManager(
        root=str(tmp_path / "lm"), catalog=catalog, probe_transport=transport,
    )
    with pytest.raises(LocalModelError) as exc:
        mgr.probe("https://api.runpod.ai/v2/xxx/openai/v1", accept_remote=True)
    assert exc.value.code == "auth"


def test_runpod_distinct_paths_coexist(tmp_path, monkeypatch):
    catalog, _, _ = _tiny_catalog(tmp_path)
    monkeypatch.setattr(
        "harness.local_model_manager.is_safe_url_pinned",
        lambda url, **k: (True, "", "1.2.3.4"),
    )

    def transport(url, **kwargs):
        return {"payload": {"data": []}, "headers": {}, "status": 200}

    mgr = LocalModelManager(
        root=str(tmp_path / "lm"), catalog=catalog, probe_transport=transport,
    )
    first = mgr.save_external(
        "https://api.runpod.ai/v2/aaa/openai/v1",
        accept_remote=True,
        model="qwen-a",
    )
    second = mgr.save_external(
        "https://api.runpod.ai/v2/bbb/openai/v1",
        accept_remote=True,
        model="qwen-b",
    )
    ids = [item["id"] for item in second["externals"]]
    assert len(ids) == 2
    assert ids[0] != ids[1]
    assert first["externals"][0]["id"] in ids


def test_download_range_end_must_be_total_minus_one(tmp_path):
    catalog, runtime_bytes, _model = _tiny_catalog(tmp_path)
    mgr = LocalModelManager(root=str(tmp_path / "lm"), catalog=catalog)
    asset = catalog["runtime"]["assets"]["linux-x64"]
    part = mgr._part_path(asset["filename"])
    os.makedirs(os.path.dirname(part), exist_ok=True)
    with open(part, "wb") as handle:
        handle.write(runtime_bytes[:4])

    def urlopen(req, timeout=None):
        return _RangeBody(
            runtime_bytes, start=4, status=206,
            headers={"Content-Range": "bytes 4-10/%s" % len(runtime_bytes)},
        )

    mgr.urlopen = urlopen
    with pytest.raises(LocalModelError) as exc:
        mgr.download_asset(asset, target="runtime")
    assert exc.value.code == "download"
    assert os.path.getsize(part) == 4


def test_download_reuses_verified_final(tmp_path):
    catalog, runtime_bytes, _model = _tiny_catalog(tmp_path)
    mgr = LocalModelManager(root=str(tmp_path / "lm"), catalog=catalog)
    asset = catalog["runtime"]["assets"]["linux-x64"]
    final = mgr._final_path(asset["filename"])
    os.makedirs(os.path.dirname(final), exist_ok=True)
    with open(final, "wb") as handle:
        handle.write(runtime_bytes)
    calls = {"n": 0}

    def urlopen(req, timeout=None):
        calls["n"] += 1
        return _RangeBody(runtime_bytes, status=200)

    mgr.urlopen = urlopen
    path = mgr.download_asset(asset, target="runtime")
    assert path == final
    assert calls["n"] == 0


def test_download_throttles_durable_progress(tmp_path):
    payload = b"x" * (8 * 1024 * 1024)
    digest = hashlib.sha256(payload).hexdigest()
    catalog, _, _ = _tiny_catalog(tmp_path)
    catalog["runtime"]["assets"]["linux-x64"] = {
        "filename": "big.bin",
        "url": "http://fixture.test/big.bin",
        "sha256": digest,
        "size": len(payload),
        "archive": "bin",
    }
    mgr = LocalModelManager(root=str(tmp_path / "lm"), catalog=catalog, clock=lambda: 1000.0)
    writes = []
    orig = mgr._set_download

    def wrapped(target, payload_row):
        writes.append(dict(payload_row))
        return orig(target, payload_row)

    mgr._set_download = wrapped
    mgr.urlopen = lambda req, timeout=None: _RangeBody(payload, status=200)
    mgr.download_asset(catalog["runtime"]["assets"]["linux-x64"], target="runtime")
    chunk_count = len(payload) / (64 * 1024)
    assert chunk_count > 20
    assert len(writes) < 10
    assert DOWNLOAD_PROGRESS_BYTES >= 4 * 1024 * 1024
    assert DOWNLOAD_PROGRESS_INTERVAL <= 0.25


def test_clear_cancel_all_clears_all_event(tmp_path):
    catalog, _, _ = _tiny_catalog(tmp_path)
    mgr = LocalModelManager(root=str(tmp_path / "lm"), catalog=catalog)
    mgr.cancel("all")
    assert mgr._cancel["all"].is_set()
    assert mgr._cancelled("runtime")
    mgr._clear_cancel("all")
    assert not mgr._cancel["all"].is_set()
    assert not mgr._cancelled("runtime")
    assert not mgr._cancelled("model")


def test_cancel_all_then_resume_download(tmp_path):
    catalog, runtime_bytes, _model = _tiny_catalog(tmp_path)
    mgr = LocalModelManager(root=str(tmp_path / "lm"), catalog=catalog)
    asset = catalog["runtime"]["assets"]["linux-x64"]

    class _Chunked:
        def __init__(self):
            self.sent = False

        def read(self, n=-1):
            if not self.sent:
                self.sent = True
                mgr.cancel("all")
                return runtime_bytes[:2]
            return runtime_bytes[2:]

        def close(self):
            return None

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    mgr.urlopen = lambda req, timeout=None: _Chunked()
    with pytest.raises(LocalModelError) as exc:
        mgr.download_asset(asset, target="runtime")
    assert exc.value.code == "cancelled"
    part = mgr._part_path(asset["filename"])
    assert os.path.exists(part)
    mgr._clear_cancel("all")
    mgr.urlopen = lambda req, timeout=None: _RangeBody(
        runtime_bytes, start=os.path.getsize(part), status=206, total=len(runtime_bytes),
    )
    path = mgr.download_asset(asset, target="runtime")
    assert sha256_file(path) == asset["sha256"]


def test_cancelled_component_is_paused_not_error(tmp_path):
    catalog, _, _ = _tiny_catalog(tmp_path)
    mgr = LocalModelManager(root=str(tmp_path / "lm"), catalog=catalog)
    mgr._install_runtime = lambda: mgr._set_component(
        "runtime", status="ready", path="/bin/x", error=None,
    )

    def cancel_model(_model_id=""):
        raise LocalModelError("Download cancelled", code="cancelled")

    mgr._install_model = cancel_model
    mgr._install_worker("all", "qwen-test")
    state = mgr._state()
    assert state["managed"]["runtime"]["status"] == "ready"
    assert state["managed"]["model"]["status"] == "paused"
    assert state["managed"]["runtime"]["status"] != "error"


def test_model_failure_does_not_mark_runtime_error(tmp_path):
    catalog, _, _ = _tiny_catalog(tmp_path)
    mgr = LocalModelManager(root=str(tmp_path / "lm"), catalog=catalog)
    mgr._install_runtime = lambda: mgr._set_component(
        "runtime", status="ready", path="/bin/x", error=None,
    )

    def fail(_model_id=""):
        raise LocalModelError("SHA-256 mismatch", code="hash_mismatch")

    mgr._install_model = fail
    mgr._install_worker("all", "qwen-test")
    state = mgr._state()
    assert state["managed"]["runtime"]["status"] == "ready"
    assert state["managed"]["model"]["status"] == "error"


def test_overlapping_install_is_rejected_while_running(tmp_path):
    catalog, runtime_bytes, _ = _tiny_catalog(tmp_path)
    started = threading.Event()
    release = threading.Event()
    mgr = LocalModelManager(root=str(tmp_path / "lm"), catalog=catalog)

    class _Blocked:
        def read(self, n=-1):
            started.set()
            release.wait(2)
            return runtime_bytes if not getattr(self, "sent", False) else b""

        def close(self):
            return None

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        @property
        def status(self):
            return 200

        headers = {}

    def urlopen(req, timeout=None):
        body = _Blocked()
        return body

    mgr.urlopen = urlopen
    first = mgr.install("runtime", background=True)
    assert started.wait(1)
    with pytest.raises(LocalModelError) as exc:
        mgr.install("runtime", background=True)
    assert exc.value.code == "busy"
    alive = [w for w in mgr._workers.values() if w is not None and w.is_alive()]
    assert len(alive) == 1
    release.set()
    alive[0].join(2)
    assert first["managed"]


def test_stop_kills_owned_handle_when_ps_fails(tmp_path, monkeypatch):
    catalog, _, _ = _tiny_catalog(tmp_path)
    stopped = []
    monkeypatch.setattr(
        "harness.local_model_manager.stop_process_tree",
        lambda pid, proc=None, **k: stopped.append((pid, proc)),
    )
    monkeypatch.setattr("harness.local_model_manager.read_process_command", lambda pid: "")
    monkeypatch.setattr("harness.local_model_manager._pid_alive", lambda pid: True)
    mgr = LocalModelManager(root=str(tmp_path / "lm"), catalog=catalog)

    class _Proc:
        pid = 4242

        def wait(self, timeout=None):
            return 0

        def kill(self):
            return None

    log = open(os.path.join(str(tmp_path), "llama.log"), "wb")
    proc = _Proc()
    mgr._procs[4242] = (proc, log)
    state = mgr._state()
    state["managed"]["process"] = {
        "pid": 4242, "port": 9, "host": "127.0.0.1",
        "exe": "/tmp/llama-server", "model_path": "/tmp/model.gguf",
        "alias": "marionette-deadbeef", "nonce": "deadbeef",
    }
    mgr._save(state)
    mgr.stop()
    assert stopped
    assert stopped[0][0] == 4242
    assert log.closed


def test_start_restarts_unhealthy_matching_process(tmp_path, monkeypatch):
    catalog, _, model_bytes = _tiny_catalog(tmp_path)
    root = tmp_path / "lm"
    mgr = LocalModelManager(
        root=str(root), catalog=catalog, sleeper=lambda _s: None,
        clock=lambda: 1.0, ready_timeout=1.0,
    )
    runtime = root / "runtime" / "test"
    runtime.mkdir(parents=True)
    exe = runtime / "llama-server"
    exe.write_text("x", encoding="utf-8")
    (root / "models").mkdir()
    model_path = root / "models" / "model.gguf"
    model_path.write_bytes(model_bytes)
    mgr._set_component("runtime", status="ready", path=str(exe))
    mgr._set_component("model", status="ready", path=str(model_path), id="qwen-test")
    monkeypatch.setattr(
        "harness.local_model_manager.process_matches_identity",
        lambda pid, ident: True,
    )
    monkeypatch.setattr(
        "harness.local_model_manager.stop_process_tree",
        lambda *a, **k: None,
    )
    state = mgr._state()
    state["managed"]["process"] = {
        "pid": 11, "port": 9, "host": "127.0.0.1",
        "exe": str(exe), "model_path": str(model_path),
        "alias": "marionette-old", "nonce": "old",
        "healthy": False,
    }
    mgr._save(state)
    spawned = []

    class _Proc:
        pid = 22

        def poll(self):
            return None

        def wait(self, timeout=None):
            return 0

        def kill(self):
            return None

    def popen(*a, **k):
        spawned.append(1)
        return _Proc()

    mgr.popen = popen
    health = {"n": 0}

    def probe(*a, **k):
        health["n"] += 1
        return health["n"] >= 2

    mgr._probe_health = probe
    snap = mgr.start()
    assert spawned
    assert snap["managed"]["process"]["pid"] == 22
    assert snap["managed"]["process"]["healthy"] is True


def test_event_log_wait_wakes_on_append():
    log = EventLog()
    seen = []

    def waiter():
        seen.extend(log.wait_since(0, timeout=2.0))

    thread = threading.Thread(target=waiter)
    thread.start()
    time.sleep(0.05)
    log.append("progress", {"n": 1})
    thread.join(2)
    assert seen
    assert seen[0]["kind"] == "progress"


def test_pid_alive_windows_does_not_signal(monkeypatch):
    signaled = []

    def fake_kill(pid, sig):
        signaled.append((pid, sig))
        raise AssertionError("Windows liveness must not call os.kill")

    monkeypatch.setattr("harness.local_model_manager._platform_name", lambda: "nt")
    monkeypatch.setattr(os, "kill", fake_kill)
    monkeypatch.setattr(
        "harness.local_model_manager._windows_pid_query",
        lambda pid: {"alive": True, "image": r"C:\\llama-server.exe", "start_key": "abc"},
    )
    assert _pid_alive(4242) is True
    assert signaled == []


def test_windows_identity_fail_closed_without_query(monkeypatch):
    monkeypatch.setattr("harness.local_model_manager._platform_name", lambda: "nt")
    monkeypatch.setattr("harness.local_model_manager._windows_pid_query", lambda pid: None)
    assert _pid_alive(9) is False
    assert read_process_command(9) == ""
    assert read_process_start_key(9) == ""
    assert process_matches_identity(9, {
        "exe": "llama-server.exe", "start_key": "abc", "alias": "marionette-x",
    }) is False


def test_windows_identity_matches_start_key_and_image(monkeypatch):
    monkeypatch.setattr("harness.local_model_manager._platform_name", lambda: "nt")
    monkeypatch.setattr(
        "harness.local_model_manager._windows_pid_query",
        lambda pid: {
            "alive": True,
            "image": r"C:\\tools\\llama-server.exe",
            "start_key": "deadbeefcafebabe",
        },
    )
    identity = {
        "exe": r"C:\\tools\\llama-server.exe",
        "start_key": "deadbeefcafebabe",
        "alias": "marionette-x",
        "nonce": "x",
        "model_path": r"C:\\models\\qwen.gguf",
    }
    assert process_matches_identity(11, identity) is True
    identity["start_key"] = "other"
    assert process_matches_identity(11, identity) is False


def test_windows_helpers_never_call_wmic(monkeypatch):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        return SimpleNamespace(returncode=1, stdout="")

    monkeypatch.setattr("harness.local_model_manager._platform_name", lambda: "nt")
    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr("harness.local_model_manager.subprocess.run", fake_run)
    monkeypatch.setattr(
        "harness.local_model_manager._windows_pid_query",
        lambda pid: {"alive": True, "image": "llama-server.exe", "start_key": "1"},
    )
    assert read_process_command(3) == "llama-server.exe"
    assert read_process_start_key(3) == "1"
    assert not any("wmic" in " ".join(item).lower() for item in calls)


def test_probe_refuses_all_redirects_including_public_origin():
    handler = ProbeRedirectHandler()
    req = urllib.request.Request("https://proxy.runpod.net/v1/models")
    with pytest.raises(urllib.error.HTTPError) as exc:
        handler.redirect_request(
            req, None, 302, "Found", {"Authorization": "Bearer secret"},
            "https://evil.example/steal",
        )
    assert "must not redirect" in str(exc.value).lower()
    assert "secret" not in str(exc.value).lower()


def test_probe_refuses_dns_rebind_public_to_loopback(monkeypatch):
    monkeypatch.setattr(
        "harness.local_model_manager.is_safe_url_pinned",
        lambda url, **k: (True, "", "127.0.0.1"),
    )
    with pytest.raises(LocalModelError) as exc:
        assert_probe_hop_safe("https://evil.example/v1", accept_remote=True)
    assert exc.value.code == "loopback"


def test_probe_http_error_does_not_echo_body(tmp_path, monkeypatch):
    catalog, _, _ = _tiny_catalog(tmp_path)

    def boom(req, timeout=None, **kwargs):
        err = urllib.error.HTTPError(
            "http://127.0.0.1:9/v1/models", 502, "bad", {}, io.BytesIO(b"internal-trace-secret"),
        )
        raise err

    monkeypatch.setattr("harness.local_model_manager.probe_urlopen", boom)
    mgr = LocalModelManager(root=str(tmp_path / "lm"), catalog=catalog)
    with pytest.raises(LocalModelError) as exc:
        mgr.probe("http://127.0.0.1:9/v1")
    assert "internal-trace-secret" not in str(exc.value)
    assert "502" in str(exc.value)


def test_empty_key_on_resave_preserves_vault_key(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path))
    catalog, _, _ = _tiny_catalog(tmp_path)
    stored = {}

    def set_key(reach, value):
        stored[reach] = value

    def status(reach):
        value = stored.get(reach) or ""
        return {"has_key": bool(value), "masked": "••••" if value else ""}

    monkeypatch.setattr("harness.keys.set_api_key", set_key)
    monkeypatch.setattr("harness.keys.get_api_key_status", status)
    mgr = LocalModelManager(
        root=str(tmp_path / "lm"),
        catalog=catalog,
        probe_transport=lambda *a, **k: {
            "payload": {"data": [{"id": "llama3"}]}, "headers": {}, "status": 200,
        },
    )
    first = mgr.save_external(
        "http://192.168.1.20:8080/v1",
        api_key="sk-secret-value",
        accept_lan=True,
        model="llama3",
    )
    blob = json.dumps(first) + json.dumps(mgr.events.since(0))
    assert "sk-secret-value" not in blob
    assert first["externals"][0]["has_key"] is True
    second = mgr.save_external(
        "http://192.168.1.20:8080/v1",
        api_key="",
        accept_lan=True,
        model="llama3",
    )
    row = second["externals"][0]
    assert row["has_key"] is True
    assert "sk-secret-value" not in json.dumps(second)
    assert stored[lm_reach(row["id"])] == "sk-secret-value"


def test_concurrent_install_and_start_rejected(tmp_path, monkeypatch):
    catalog, _, _ = _tiny_catalog(tmp_path)
    monkeypatch.setattr(
        "harness.local_model_manager.detect_hardware",
        lambda *_a, **_k: {"supported": True},
    )
    mgr = LocalModelManager(root=str(tmp_path / "lm"), catalog=catalog)
    hold = threading.Event()

    def blocker():
        hold.wait(2)

    worker = threading.Thread(target=blocker, daemon=True)
    worker.start()
    mgr._workers["all"] = worker
    with pytest.raises(LocalModelError) as exc:
        mgr.install("all", model_id="qwen-test")
    assert exc.value.code == "busy"
    mgr._start_in_progress = True
    with pytest.raises(LocalModelError) as exc:
        mgr.start()
    assert exc.value.code == "busy"
    hold.set()
    worker.join(1)


def test_cpu_asset_keeps_ngl_zero_when_nvidia_present(tmp_path, monkeypatch):
    catalog, _, model_bytes = _tiny_catalog(tmp_path)
    for key, asset in catalog["runtime"]["assets"].items():
        asset["backend"] = "cpu"
        asset["platform"] = key
    monkeypatch.setattr("harness.local_models.detect_accelerator", lambda: "cuda")
    monkeypatch.setattr("harness.local_model_manager.shutil.which", lambda name: "/usr/bin/nvidia-smi")
    root = tmp_path / "lm"
    mgr = LocalModelManager(
        root=str(root), catalog=catalog, sleeper=lambda _s: None,
        clock=lambda: 1.0, ready_timeout=1.0,
    )
    runtime = root / "runtime" / "test"
    runtime.mkdir(parents=True)
    exe = runtime / "llama-server"
    exe.write_text("x", encoding="utf-8")
    (root / "models").mkdir()
    model_path = root / "models" / "model.gguf"
    model_path.write_bytes(model_bytes)
    mgr._set_component("runtime", status="ready", path=str(exe), platform="linux-x64")
    mgr._set_component("model", status="ready", path=str(model_path), id="qwen-test")
    spawned = {}

    class _Proc:
        pid = 77

        def wait(self, timeout=None):
            return 0

        def kill(self):
            return None

    mgr.popen = lambda argv, **kwargs: spawned.update(argv=argv) or _Proc()
    mgr._probe_health = lambda *a, **k: True
    mgr.start()
    argv = spawned["argv"]
    assert argv[argv.index("-ngl") + 1] == "0"


def test_metal_asset_keeps_ngl_99(tmp_path, monkeypatch):
    catalog, _, model_bytes = _tiny_catalog(tmp_path)
    for key, asset in catalog["runtime"]["assets"].items():
        asset["backend"] = "metal"
    root = tmp_path / "lm"
    mgr = LocalModelManager(
        root=str(root), catalog=catalog, sleeper=lambda _s: None,
        clock=lambda: 1.0, ready_timeout=1.0,
    )
    runtime = root / "runtime" / "test"
    runtime.mkdir(parents=True)
    exe = runtime / "llama-server"
    exe.write_text("x", encoding="utf-8")
    (root / "models").mkdir()
    model_path = root / "models" / "model.gguf"
    model_path.write_bytes(model_bytes)
    mgr._set_component("runtime", status="ready", path=str(exe), platform="macos-arm64")
    mgr._set_component("model", status="ready", path=str(model_path), id="qwen-test")
    spawned = {}

    class _Proc:
        pid = 78

        def wait(self, timeout=None):
            return 0

        def kill(self):
            return None

    mgr.popen = lambda argv, **kwargs: spawned.update(argv=argv) or _Proc()
    mgr._probe_health = lambda *a, **k: True
    mgr.start()
    argv = spawned["argv"]
    assert argv[argv.index("-ngl") + 1] == "99"


def test_probe_uses_one_dns_pin(tmp_path, monkeypatch):
    catalog, _, _ = _tiny_catalog(tmp_path)
    pins = []

    def pinned(url, **k):
        pins.append(url)
        return True, "", "1.2.3.4"

    monkeypatch.setattr("harness.local_model_manager.is_safe_url_pinned", pinned)

    class _Resp:
        status = 200
        headers = {}

        def __init__(self):
            self._buf = io.BytesIO(b'{"data":[{"id":"qwen"}]}')

        def read(self, n=-1):
            return self._buf.read(n)

        def close(self):
            return None

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    seen_pin = []

    def fake_urlopen(req, timeout=None, **kwargs):
        seen_pin.append(kwargs.get("pinned_ip"))
        return _Resp()

    monkeypatch.setattr("harness.local_model_manager.probe_urlopen", fake_urlopen)
    mgr = LocalModelManager(root=str(tmp_path / "lm"), catalog=catalog)
    result = mgr.probe("https://models.example/v1", accept_remote=True)
    assert result["ok"] is True
    assert len(pins) == 1
    assert seen_pin
    assert all(ip == "1.2.3.4" for ip in seen_pin)


def test_probe_urlopen_reuses_pin_without_second_dns(monkeypatch):
    calls = []

    def pinned(url, **k):
        calls.append(url)
        return True, "", "9.9.9.9"

    monkeypatch.setattr("harness.local_model_manager.is_safe_url_pinned", pinned)
    seen = []

    class _Opener:
        def open(self, req, timeout=None):
            return SimpleNamespace(status=200)

    def build_opener(*handlers):
        for handler in handlers:
            pin = getattr(handler, "_pin", None)
            if pin is not None:
                seen.append(getattr(pin, "ip", None))
        return _Opener()

    monkeypatch.setattr(urllib.request, "build_opener", build_opener)
    req = urllib.request.Request("https://models.example/v1/models")
    probe_urlopen(req, accept_remote=True, pinned_ip="1.2.3.4")
    assert calls == []
    assert "1.2.3.4" in seen


def test_windows_unmatched_pid_does_not_taskkill(tmp_path, monkeypatch):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("harness.local_model_manager._platform_name", lambda: "nt")
    monkeypatch.setattr("harness.local_model_manager.subprocess.run", fake_run)
    monkeypatch.setattr(
        "harness.local_model_manager._windows_pid_query",
        lambda pid: {
            "alive": True,
            "image": r"C:\\Windows\\notepad.exe",
            "start_key": "ffff",
        },
    )
    catalog, _, _ = _tiny_catalog(tmp_path)
    mgr = LocalModelManager(root=str(tmp_path / "lm"), catalog=catalog)
    state = mgr._state()
    state["managed"]["process"] = {
        "pid": 4242, "port": 9, "host": "127.0.0.1",
        "exe": r"C:\\tools\\llama-server.exe",
        "model_path": r"C:\\models\\qwen.gguf",
        "alias": "marionette-x",
        "nonce": "x",
        "start_key": "deadbeefcafebabe",
    }
    mgr._save(state)
    mgr.stop()
    assert not any(call and call[0] == "taskkill" for call in calls)
    assert mgr._state()["managed"]["process"] is None


def test_concurrent_start_second_is_busy(tmp_path, monkeypatch):
    catalog, _, model_bytes = _tiny_catalog(tmp_path)
    monkeypatch.setattr(
        "harness.local_model_manager.detect_hardware",
        lambda *_a, **_k: {"supported": True},
    )
    root = tmp_path / "lm"
    mgr = LocalModelManager(
        root=str(root), catalog=catalog, sleeper=lambda _s: None,
        clock=lambda: 1.0, ready_timeout=1.0,
    )
    runtime = root / "runtime" / "test"
    runtime.mkdir(parents=True)
    exe = runtime / "llama-server"
    exe.write_text("x", encoding="utf-8")
    (root / "models").mkdir()
    model_path = root / "models" / "model.gguf"
    model_path.write_bytes(model_bytes)
    mgr._set_component("runtime", status="ready", path=str(exe), platform="linux-x64")
    mgr._set_component("model", status="ready", path=str(model_path), id="qwen-test")
    started = threading.Event()
    release = threading.Event()

    class _Proc:
        pid = 91

        def wait(self, timeout=None):
            return 0

        def kill(self):
            return None

    def popen(*_a, **_k):
        started.set()
        release.wait(2)
        return _Proc()

    mgr.popen = popen
    mgr._probe_health = lambda *_a, **_k: True
    errors = []

    def first():
        mgr.start()

    def second():
        if not started.wait(1):
            errors.append("first-never-started")
            return
        try:
            mgr.start()
        except LocalModelError as exc:
            errors.append(exc.code)

    t1 = threading.Thread(target=first)
    t2 = threading.Thread(target=second)
    t1.start()
    t2.start()
    t2.join(3)
    release.set()
    t1.join(3)
    assert errors == ["busy"]


def test_snapshot_and_save_do_not_deadlock(tmp_path, monkeypatch):
    catalog, _, _ = _tiny_catalog(tmp_path)
    monkeypatch.setattr(
        "harness.local_model_manager.is_safe_url_pinned",
        lambda url, **k: (True, "", "127.0.0.1"),
    )
    mgr = LocalModelManager(
        root=str(tmp_path / "lm"),
        catalog=catalog,
        probe_transport=lambda *_a, **_k: {
            "payload": {"data": [{"id": "llama3"}]},
            "headers": {},
            "status": 200,
        },
    )
    barrier = threading.Barrier(2)
    done = []

    def snap():
        barrier.wait()
        for _ in range(20):
            mgr.snapshot()
        done.append("snap")

    def save():
        barrier.wait()
        for _ in range(8):
            mgr.save_external("http://127.0.0.1:11434/v1", model="llama3")
        done.append("save")

    t1 = threading.Thread(target=snap)
    t2 = threading.Thread(target=save)
    t1.start()
    t2.start()
    t1.join(5)
    t2.join(5)
    assert not t1.is_alive() and not t2.is_alive()
    assert sorted(done) == ["save", "snap"]


def _seed_external(mgr, **fields):
    state = mgr._state()
    row = {
        "id": "ollama-127-0-0-1-11434",
        "name": "ollama",
        "vendor": "ollama",
        "base_url": "http://127.0.0.1:11434/v1",
        "models": ["llama3"],
        "selected_model": "llama3",
        "healthy": True,
        "has_key": False,
        "kind": "loopback",
        "requires_key": False,
        "lan_accepted": False,
        "remote_accepted": False,
    }
    row.update(fields)
    state["externals"] = [row]
    mgr._save(state)
    return row


def _tool_call_payload(name="marionette_capability_probe", arguments="{\"ok\":true}"):
    return {
        "choices": [{
            "message": {
                "tool_calls": [{
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": name, "arguments": arguments},
                }],
            },
        }],
    }


def test_probe_and_save_do_not_verify_tool_calling(tmp_path):
    catalog, _, _ = _tiny_catalog(tmp_path)
    urls = []

    def transport(url, **kwargs):
        urls.append(url)
        return {"payload": {"data": [{"id": "llama3"}]}, "headers": {}, "status": 200}

    mgr = LocalModelManager(
        root=str(tmp_path / "lm"), catalog=catalog, probe_transport=transport,
    )
    mgr.probe("http://127.0.0.1:11434/v1")
    snap = mgr.save_external("http://127.0.0.1:11434/v1", model="llama3")
    assert all("/chat/completions" not in url for url in urls)
    assert snap["externals"][0]["tool_calling"]["status"] == "unverified"


def test_verify_tool_calling_verified_uses_synthetic_bearer(tmp_path):
    catalog, _, _ = _tiny_catalog(tmp_path)
    seen = []

    def transport(url, **kwargs):
        seen.append((url, kwargs))
        body = json.loads(kwargs.get("body") or b"{}")
        assert body["stream"] is False
        assert kwargs.get("method") == "POST"
        assert (kwargs.get("headers") or {}).get("Authorization") == "Bearer local"
        return {"payload": _tool_call_payload(), "headers": {}, "status": 200}

    mgr = LocalModelManager(
        root=str(tmp_path / "lm"), catalog=catalog, probe_transport=transport,
    )
    _seed_external(mgr)
    snap = mgr.verify_tool_calling("local:ollama-127-0-0-1-11434/llama3")
    row = snap["externals"][0]
    assert row["tool_calling"]["status"] == "verified"
    assert row["healthy"] is True
    assert row.get("last_error") in (None, "")
    assert seen and "/chat/completions" in seen[0][0]


def test_verify_tool_calling_unsupported_keeps_endpoint_healthy(tmp_path):
    catalog, _, _ = _tiny_catalog(tmp_path)

    def transport(url, **kwargs):
        return {
            "payload": {"choices": [{"message": {"content": "I cannot call functions."}}]},
            "headers": {},
            "status": 200,
        }

    mgr = LocalModelManager(
        root=str(tmp_path / "lm"), catalog=catalog, probe_transport=transport,
    )
    _seed_external(mgr)
    snap = mgr.verify_tool_calling("local:ollama-127-0-0-1-11434/llama3")
    row = snap["externals"][0]
    assert row["tool_calling"]["status"] == "unsupported"
    assert "text" in row["tool_calling"]["reason"]
    assert row["healthy"] is True
    usable = mgr.usable_specs()
    assert "local:ollama-127-0-0-1-11434/llama3" in usable


def test_verify_tool_calling_malformed_is_error(tmp_path):
    catalog, _, _ = _tiny_catalog(tmp_path)

    def transport(url, **kwargs):
        return {"payload": {"choices": [{"message": {"tool_calls": "nope"}}]}, "headers": {}, "status": 200}

    mgr = LocalModelManager(
        root=str(tmp_path / "lm"), catalog=catalog, probe_transport=transport,
    )
    _seed_external(mgr)
    snap = mgr.verify_tool_calling("local:ollama-127-0-0-1-11434/llama3")
    row = snap["externals"][0]
    assert row["tool_calling"]["status"] == "error"
    assert "malformed" in row["tool_calling"]["reason"].lower()
    assert row["healthy"] is True


def test_verify_tool_calling_auth_is_error(tmp_path):
    catalog, _, _ = _tiny_catalog(tmp_path)

    def transport(url, **kwargs):
        return {"payload": {}, "headers": {}, "status": 401}

    mgr = LocalModelManager(
        root=str(tmp_path / "lm"), catalog=catalog, probe_transport=transport,
    )
    _seed_external(mgr)
    snap = mgr.verify_tool_calling("local:ollama-127-0-0-1-11434/llama3")
    row = snap["externals"][0]
    assert row["tool_calling"]["status"] == "error"
    assert "API key" in row["tool_calling"]["reason"]
    assert row["healthy"] is True


def test_verify_tool_calling_transport_is_error(tmp_path):
    catalog, _, _ = _tiny_catalog(tmp_path)

    def transport(url, **kwargs):
        raise LocalModelError("Endpoint probe failed", code="probe")

    mgr = LocalModelManager(
        root=str(tmp_path / "lm"), catalog=catalog, probe_transport=transport,
    )
    _seed_external(mgr)
    snap = mgr.verify_tool_calling("local:ollama-127-0-0-1-11434/llama3")
    row = snap["externals"][0]
    assert row["tool_calling"]["status"] == "error"
    assert "could not be reached" in row["tool_calling"]["reason"]
    assert row["healthy"] is True


def test_verify_public_without_key_fails_closed(tmp_path):
    catalog, _, _ = _tiny_catalog(tmp_path)
    called = []

    def transport(url, **kwargs):
        called.append(url)
        raise AssertionError("public verify must not send a request without a key")

    mgr = LocalModelManager(
        root=str(tmp_path / "lm"), catalog=catalog, probe_transport=transport,
    )
    _seed_external(
        mgr,
        id="runpod-box",
        base_url="https://proxy.runpod.net/v1",
        kind="public",
        requires_key=True,
        remote_accepted=True,
        has_key=False,
    )
    snap = mgr.verify_tool_calling("local:runpod-box/llama3")
    row = snap["externals"][0]
    assert row["tool_calling"]["status"] == "error"
    assert row["tool_calling"]["reason"] == "This public endpoint requires an API key."
    assert row["healthy"] is True
    assert called == []


def test_verify_does_not_persist_or_echo_secret(tmp_path, monkeypatch):
    catalog, _, _ = _tiny_catalog(tmp_path)
    monkeypatch.setattr(
        "harness.local_model_manager.is_safe_url_pinned",
        lambda url, **k: (True, "", "1.2.3.4"),
    )
    monkeypatch.setenv("LOCAL_RUNPOD_BOX_API_KEY", "sk-secret-value")
    monkeypatch.setattr("harness.keys.hydrate_reach_key", lambda _reach: True)
    monkeypatch.setattr(
        "harness.keys.get_env_var_for_reach",
        lambda _reach: "LOCAL_RUNPOD_BOX_API_KEY",
    )

    def transport(url, **kwargs):
        assert "sk-secret-value" in (kwargs.get("headers") or {}).get("Authorization", "")
        return {"payload": _tool_call_payload(), "headers": {}, "status": 200}

    mgr = LocalModelManager(
        root=str(tmp_path / "lm"), catalog=catalog, probe_transport=transport,
    )
    _seed_external(
        mgr,
        id="runpod-box",
        base_url="https://proxy.runpod.net/v1",
        kind="public",
        requires_key=True,
        remote_accepted=True,
        has_key=True,
    )
    snap = mgr.verify_tool_calling("local:runpod-box/llama3")
    blob = json.dumps(snap) + json.dumps(mgr.events.since(0))
    assert "sk-secret-value" not in blob
    assert snap["externals"][0]["tool_calling"]["status"] == "verified"
    assert snap["externals"][0].get("api_key") in (None, "", "••••")


def test_verify_never_executes_returned_function(tmp_path):
    catalog, _, _ = _tiny_catalog(tmp_path)
    arguments = "{\"cmd\":\"rm -rf /\",\"code\":\"__import__('os').system('id')\"}"

    def transport(url, **kwargs):
        return {
            "payload": _tool_call_payload(arguments=arguments),
            "headers": {},
            "status": 200,
        }

    mgr = LocalModelManager(
        root=str(tmp_path / "lm"), catalog=catalog, probe_transport=transport,
    )
    _seed_external(mgr)
    snap = mgr.verify_tool_calling("local:ollama-127-0-0-1-11434/llama3")
    blob = json.dumps(snap) + json.dumps(mgr.events.since(0))
    assert snap["externals"][0]["tool_calling"]["status"] == "verified"
    assert "rm -rf" not in blob
    assert "__import__" not in blob
    assert arguments not in blob


def test_verify_rejects_managed_spec(tmp_path):
    catalog, _, _ = _tiny_catalog(tmp_path)
    mgr = LocalModelManager(root=str(tmp_path / "lm"), catalog=catalog)
    with pytest.raises(LocalModelError) as exc:
        mgr.verify_tool_calling("local:managed/qwen3-4b")
    assert exc.value.code == "managed"
