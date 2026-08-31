"""Sole writer for local-model downloads, llama-server ownership, and probes.

Injection seams keep tests hermetic: callers supply urlopen / popen /
probe_transport so the suite never hits the network or a real llama-server.
Production install uses the dedicated HTTPS opener with no env gate.
Identity-safe PID adoption never kills an unrelated process.
"""
from __future__ import annotations

import atexit
import hashlib
import json
import os
import random
import re
import signal
import shutil
import socket
import stat
import subprocess
import tarfile
import threading
import time
import zipfile
from typing import Any, Callable, Optional
from urllib.parse import urlparse
import urllib.error
import urllib.request

from .local_models import (
    MANAGED_ENDPOINT_ID,
    _ip_kind,
    bound_plain_reason,
    cached_catalog,
    canonical_spec,
    classify_tool_calling_payload,
    curated_model,
    empty_tool_calling,
    detect_hardware,
    detect_vendor_from_probe,
    detect_vendor_from_url,
    endpoint_id_for_url,
    evaluate_endpoint_url,
    extract_context_length,
    extract_model_ids,
    load_state,
    local_secret_reach,
    parse_local_spec,
    redact_mapping,
    resolve_local_endpoint,
    runtime_asset_for_platform,
    runtime_offload_layers,
    save_state,
    snapshot_from_state,
    state_root,
    tool_calling_error_reason,
    tool_calling_request_body,
    usable_local_specs,
)
from .url_safety import is_safe_url_pinned, normalize_url_for_request
from .web_tools import _PinnedIP, _PinnedIPHTTPHandler, _PinnedIPHTTPSHandler

UrlOpen = Callable[..., Any]
PopenFactory = Callable[..., Any]

PROBE_MAX_BYTES = 1024 * 1024
DOWNLOAD_PROGRESS_BYTES = 4 * 1024 * 1024
DOWNLOAD_PROGRESS_INTERVAL = 0.25
DOWNLOAD_HOSTS = frozenset({
    "github.com",
    "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",
    "huggingface.co",
})
_CONTENT_RANGE_RE = re.compile(r"bytes\s+(\d+)-(\d+)/(\d+|\*)", re.IGNORECASE)
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
_CREATE_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
_DETACHED_PROCESS = getattr(subprocess, "DETACHED_PROCESS", 0x00000008)


def _platform_name() -> str:
    return os.name


class LocalModelError(RuntimeError):
    def __init__(self, message: str, *, code: str = "error", http_status: Optional[int] = None) -> None:
        super().__init__(message)
        self.code = code
        self.http_status = http_status


def download_host_allowed(host: str) -> bool:
    folded = (host or "").lower().rstrip(".")
    if not folded:
        return False
    if folded in DOWNLOAD_HOSTS:
        return True
    return folded == "hf.co" or folded.endswith(".hf.co")


def validate_download_url(url: str) -> Optional[str]:
    """HTTPS + allowlisted host + no private/metadata hop. Returns pinned IP."""
    parsed = urlparse(url)
    if (parsed.scheme or "").lower() != "https":
        raise LocalModelError("Downloads must use HTTPS", code="unsafe_url")
    host = (parsed.hostname or "").lower().rstrip(".")
    if not download_host_allowed(host):
        raise LocalModelError("Download host is not on the allowlist", code="unsafe_url")
    ok, reason, pinned_ip = is_safe_url_pinned(url, allow_private=False)
    if not ok:
        raise LocalModelError(reason or "Download URL is blocked", code="unsafe_url")
    if pinned_ip:
        return pinned_ip.split("%")[0]
    return None


class AssetRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Re-validate every download hop: HTTPS, allowlist, no private/metadata."""

    max_redirections = 5

    def __init__(self, pin: Optional[_PinnedIP] = None, *args, **kwargs):
        self._pin = pin
        super().__init__(*args, **kwargs)

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        try:
            pinned_ip = validate_download_url(newurl)
        except LocalModelError as exc:
            raise urllib.error.HTTPError(newurl, code, str(exc), headers, fp) from exc
        if self._pin is not None and pinned_ip:
            self._pin.ip = pinned_ip
        return super().redirect_request(
            req, fp, code, msg, headers, normalize_url_for_request(newurl),
        )


def asset_download_urlopen(req, timeout=None):
    url = getattr(req, "full_url", None) or getattr(req, "get_full_url", lambda: req)()
    pinned_ip = validate_download_url(str(url))
    pin = _PinnedIP(pinned_ip)
    opener = urllib.request.build_opener(
        _PinnedIPHTTPSHandler(pin=pin),
        AssetRedirectHandler(pin=pin),
    )
    return opener.open(req, timeout=timeout)


def _default_urlopen(req, timeout=None):
    return asset_download_urlopen(req, timeout=timeout)


def parse_content_range(value: str) -> Optional[tuple]:
    match = _CONTENT_RANGE_RE.match((value or "").strip())
    if not match:
        return None
    start, end, total = match.group(1), match.group(2), match.group(3)
    if total == "*":
        return int(start), int(end), None
    return int(start), int(end), int(total)


def _response_status(resp) -> int:
    for attr in ("status", "code"):
        value = getattr(resp, attr, None)
        if value is not None:
            try:
                return int(value)
            except (TypeError, ValueError):
                pass
    return 200


def _response_header(resp, name: str) -> str:
    headers = getattr(resp, "headers", None)
    if headers is None:
        return ""
    getter = getattr(headers, "get", None)
    if callable(getter):
        return str(getter(name) or getter(name.lower()) or "")
    if isinstance(headers, dict):
        return str(headers.get(name) or headers.get(name.lower()) or "")
    return ""


def assert_probe_hop_safe(
    url: str,
    *,
    accept_lan: bool = False,
    accept_remote: bool = False,
) -> dict:
    """Classify *url* and refuse metadata / spoofed / unconfirmed hops."""
    decision = evaluate_endpoint_url(
        url, accept_lan=accept_lan, accept_remote=accept_remote,
    )
    if not decision.get("ok"):
        raise LocalModelError(
            decision.get("error") or "Unsafe endpoint",
            code=str(decision.get("kind") or "url"),
        )
    allow_private = decision.get("kind") != "public"
    ok, reason, pinned_ip = is_safe_url_pinned(url, allow_private=allow_private)
    if not ok:
        raise LocalModelError(reason or "Unsafe endpoint", code="url")
    proven_ip = pinned_ip.split("%")[0] if pinned_ip else ""
    if proven_ip:
        kind = _ip_kind(proven_ip)
        declared = str(decision.get("kind") or "")
        if kind in {"metadata", "blocked"}:
            raise LocalModelError(
                "Endpoint resolved to a blocked address",
                code=kind,
            )
        if declared == "loopback" and kind != "loopback":
            raise LocalModelError(
                "Endpoint resolved to a blocked address",
                code=kind,
            )
        if declared == "public" and kind != "public":
            raise LocalModelError(
                "Remote endpoint resolved to a private or loopback address",
                code=kind,
            )
        if declared in {"lan", "link_local"} and kind not in {"lan", "link_local"}:
            raise LocalModelError(
                "Endpoint resolved to a blocked address",
                code=kind,
            )
        if kind == "public" and not accept_remote:
            raise LocalModelError(
                "Endpoint resolved to a blocked address",
                code="public",
            )
        if kind in {"lan", "link_local"} and not accept_lan:
            raise LocalModelError(
                "This looks like a LAN address. Confirm it is a machine you trust.",
                code=kind,
            )
    if not proven_ip:
        raise LocalModelError("Endpoint address could not be pinned", code="url")
    decision = dict(decision)
    decision["pinned_ip"] = proven_ip
    return decision


class ProbeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse probe redirects. /models must not hop hosts, kinds, or Authorization."""

    max_redirections = 0

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(
            newurl, code, "Probe endpoints must not redirect", headers, fp,
        )


def probe_urlopen(
    req,
    timeout=None,
    *,
    accept_lan: bool = False,
    accept_remote: bool = False,
    pinned_ip: Optional[str] = None,
):
    """Connect using exactly one policy-validated pin. Never re-resolves DNS."""
    url = getattr(req, "full_url", None) or getattr(req, "get_full_url", lambda: req)()
    proven = str(pinned_ip or "").split("%")[0]
    if not proven:
        decision = assert_probe_hop_safe(
            str(url), accept_lan=accept_lan, accept_remote=accept_remote,
        )
        proven = str(decision.get("pinned_ip") or "").split("%")[0]
    if not proven:
        raise LocalModelError("Endpoint address could not be pinned", code="url")
    pin = _PinnedIP(proven)
    opener = urllib.request.build_opener(
        _PinnedIPHTTPHandler(pin=pin),
        _PinnedIPHTTPSHandler(pin=pin),
        ProbeRedirectHandler(),
    )
    return opener.open(req, timeout=timeout)


def read_bounded(resp, limit: int = PROBE_MAX_BYTES) -> bytes:
    chunks = []
    total = 0
    while True:
        chunk = resp.read(min(64 * 1024, max(1, limit - total + 1)))
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise LocalModelError("Endpoint response exceeded size limit", code="probe")
        chunks.append(chunk)
    return b"".join(chunks)


def spawn_popen_kwargs() -> dict:
    """POSIX new session; Windows process group + hidden console. Never DETACHED."""
    kwargs = {}
    if _platform_name() == "nt":
        kwargs["creationflags"] = _CREATE_NEW_PROCESS_GROUP | _CREATE_NO_WINDOW
        startup = getattr(subprocess, "STARTUPINFO", None)
        if startup is not None:
            info = startup()
            info.dwFlags |= getattr(subprocess, "STARTF_USESHOWWINDOW", 0)
            info.wShowWindow = 0
            kwargs["startupinfo"] = info
    else:
        kwargs["start_new_session"] = True
    return kwargs


def stop_process_tree(pid: int, proc: Any = None, *, grace: float = 5.0, sleeper=None) -> None:
    """TERM/taskkill the owned tree, then force-kill after *grace*. Never raises."""
    sleep = sleeper or time.sleep
    if not pid or int(pid) <= 1:
        return
    if _platform_name() == "nt":
        flags = _CREATE_NO_WINDOW
        try:
            subprocess.run(
                ["taskkill", "/PID", str(int(pid)), "/T"],
                capture_output=True,
                timeout=15,
                creationflags=flags,
            )
        except Exception:
            pass
        waited = False
        if proc is not None:
            try:
                proc.wait(timeout=grace)
                waited = True
            except Exception:
                waited = False
        if not waited:
            try:
                sleep(min(max(grace, 0.0), 5.0) if proc is None else 0)
            except Exception:
                pass
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(int(pid)), "/T", "/F"],
                    capture_output=True,
                    timeout=15,
                    creationflags=flags,
                )
            except Exception:
                pass
        return
    try:
        pgid = os.getpgid(int(pid))
    except (OSError, AttributeError):
        pgid = int(pid)
    try:
        os.killpg(pgid, signal.SIGTERM)
    except (OSError, AttributeError):
        try:
            os.kill(int(pid), signal.SIGTERM)
        except OSError:
            pass
    waited = False
    if proc is not None:
        try:
            proc.wait(timeout=grace)
            waited = True
        except Exception:
            waited = False
    if waited:
        return
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (OSError, AttributeError):
        try:
            os.kill(int(pid), signal.SIGKILL)
        except OSError:
            pass


def find_free_port(host: str = "127.0.0.1", preferred: Optional[int] = None) -> int:
    if preferred:
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            probe.bind((host, int(preferred)))
            probe.close()
            return int(preferred)
        except OSError:
            probe.close()
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind((host, 0))
    port = int(sock.getsockname()[1])
    sock.close()
    return port


def is_safe_archive_member(name: str) -> bool:
    text = str(name or "").replace("\\", "/")
    if not text or text.startswith("/") or text.startswith("../"):
        return False
    parts = [part for part in text.split("/") if part not in ("", ".")]
    if any(part == ".." for part in parts):
        return False
    if os.path.isabs(name) or os.path.isabs(text):
        return False
    return True


def zip_is_symlink(info: zipfile.ZipInfo) -> bool:
    mode = (info.external_attr >> 16) & 0xFFFF
    return bool(mode and stat.S_ISLNK(mode))


def tar_member_rejected(member: tarfile.TarInfo) -> Optional[str]:
    if not is_safe_archive_member(member.name):
        return "escape"
    if member.issym() or member.islnk():
        return "link"
    if member.ischr() or member.isblk() or member.isfifo() or member.isdev():
        return "device"
    if not (member.isfile() or member.isdir()):
        return "type"
    return None


def extract_archive(archive_path: str, dest_dir: str) -> str:
    """Validate then extract into staging; promote only after every member is safe."""
    os.makedirs(os.path.dirname(dest_dir) or ".", exist_ok=True)
    staging = dest_dir + ".extract"
    if os.path.exists(staging):
        shutil.rmtree(staging, ignore_errors=True)
    os.makedirs(staging, exist_ok=True)
    lower = archive_path.lower()
    try:
        if lower.endswith(".zip"):
            with zipfile.ZipFile(archive_path) as zf:
                members = []
                for info in zf.infolist():
                    if not is_safe_archive_member(info.filename):
                        raise LocalModelError(
                            "Archive member escapes the extract directory",
                            code="unsafe_archive",
                        )
                    if zip_is_symlink(info):
                        raise LocalModelError(
                            "Archive contains a symbolic link",
                            code="unsafe_archive",
                        )
                    members.append(info)
                for info in members:
                    zf.extract(info, staging)
        else:
            with tarfile.open(archive_path, "r:*") as tf:
                members = []
                for member in tf.getmembers():
                    reason = tar_member_rejected(member)
                    if reason:
                        raise LocalModelError(
                            "Archive member is not a regular file or directory",
                            code="unsafe_archive",
                        )
                    members.append(member)
                tf.extractall(staging, members=members)
        if os.path.exists(dest_dir):
            shutil.rmtree(dest_dir, ignore_errors=True)
        os.replace(staging, dest_dir)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return dest_dir


def find_binary(root: str, name: str) -> Optional[str]:
    expected = os.path.basename(name) + (".exe" if _platform_name() == "nt" and not name.endswith(".exe") else "")
    root_real = os.path.realpath(root)
    for dirpath, _dirnames, filenames in os.walk(root):
        if expected not in filenames:
            continue
        path = os.path.join(dirpath, expected)
        real = os.path.realpath(path)
        if real != root_real and not real.startswith(root_real + os.sep):
            continue
        try:
            mode = os.stat(path).st_mode
            os.chmod(path, mode | stat.S_IXUSR)
        except Exception:
            pass
        return path
    return None


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _windows_pid_query(pid: int) -> Optional[dict]:
    """Non-signaling Windows process query. Returns None when identity is unproven."""
    if not pid or int(pid) <= 0:
        return None
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            return None
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return None
            if int(code.value) != STILL_ACTIVE:
                return {"alive": False, "image": "", "start_key": ""}

            class _FILETIME(ctypes.Structure):
                _fields_ = [
                    ("dwLowDateTime", wintypes.DWORD),
                    ("dwHighDateTime", wintypes.DWORD),
                ]

            created = _FILETIME()
            exited = _FILETIME()
            kernel = _FILETIME()
            user = _FILETIME()
            start_key = ""
            if kernel32.GetProcessTimes(
                handle,
                ctypes.byref(created),
                ctypes.byref(exited),
                ctypes.byref(kernel),
                ctypes.byref(user),
            ):
                start_key = "%08x%08x" % (
                    int(created.dwHighDateTime), int(created.dwLowDateTime),
                )
            size = wintypes.DWORD(32768)
            buf = ctypes.create_unicode_buffer(size.value)
            image = ""
            if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                image = buf.value or ""
            return {"alive": True, "image": image, "start_key": start_key}
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return None


def _pid_alive(pid: int) -> bool:
    if not pid or pid <= 0:
        return False
    if _platform_name() == "nt":
        info = _windows_pid_query(int(pid))
        return bool(info and info.get("alive"))
    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False
    except Exception:
        return False


def read_process_start_key(pid: int) -> str:
    """Best-effort process birth token without a third-party dependency."""
    if not pid:
        return ""
    if _platform_name() == "nt":
        info = _windows_pid_query(int(pid))
        return str((info or {}).get("start_key") or "")
    stat_path = "/proc/%s/stat" % pid
    if os.path.exists(stat_path):
        try:
            with open(stat_path, encoding="utf-8", errors="replace") as handle:
                text = handle.read()
            close = text.rfind(")")
            if close != -1:
                fields = text[close + 2:].split()
                if len(fields) >= 20:
                    return fields[19]
        except Exception:
            pass
    try:
        proc = subprocess.run(
            ["ps", "-p", str(pid), "-o", "lstart="],
            capture_output=True,
            text=True,
            timeout=2,
        )
        if proc.returncode == 0:
            return (proc.stdout or "").strip()
    except Exception:
        pass
    return ""


def read_process_command(pid: int) -> str:
    if not pid:
        return ""
    if _platform_name() == "nt":
        info = _windows_pid_query(int(pid))
        return str((info or {}).get("image") or "")
    if os.path.exists("/proc/%s/cmdline" % pid):
        try:
            with open("/proc/%s/cmdline" % pid, "rb") as handle:
                return handle.read().replace(b"\x00", b" ").decode("utf-8", "replace")
        except Exception:
            return ""
    try:
        proc = subprocess.run(
            ["ps", "-p", str(pid), "-o", "args="],
            capture_output=True,
            text=True,
            timeout=2,
        )
        if proc.returncode == 0:
            return (proc.stdout or "").strip()
    except Exception:
        pass
    return ""


def process_matches_identity(pid: int, identity: dict) -> bool:
    """True only when the live process still looks like our llama-server."""
    if not _pid_alive(pid):
        return False
    command = read_process_command(pid)
    expected_birth = str(identity.get("start_key") or "")
    live_birth = read_process_start_key(pid)
    exe = os.path.basename(str(identity.get("exe") or ""))
    alias = str(identity.get("alias") or "")
    nonce = str(identity.get("nonce") or "")
    model_name = os.path.basename(str(identity.get("model_path") or ""))
    if _platform_name() == "nt":
        if not command or not live_birth or not expected_birth:
            return False
        if live_birth != expected_birth:
            return False
        folded = command.lower()
        if exe and exe.lower() not in folded and "llama-server" not in folded:
            return False
        return True
    if not command:
        return False
    if alias and alias not in command:
        return False
    if nonce and nonce not in command:
        return False
    if exe and exe not in command and "llama-server" not in command:
        return False
    if model_name and model_name not in command:
        return False
    if expected_birth:
        if live_birth and live_birth != expected_birth:
            return False
    return True


class EventLog:
    """In-memory resumable event log seeded from a durable cursor."""

    def __init__(self, cap: int = 256, cursor: int = 0) -> None:
        self.cap = cap
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._events = []
        self.cursor = max(0, int(cursor or 0))

    def append(self, kind: str, data: Optional[dict] = None) -> dict:
        with self._cond:
            self.cursor += 1
            event = {
                "cursor": self.cursor,
                "kind": kind,
                "data": redact_mapping(data or {}),
                "ts": time.time(),
            }
            self._events.append(event)
            if len(self._events) > self.cap:
                self._events = self._events[-self.cap:]
            self._cond.notify_all()
            return event

    def since(self, cursor: int) -> list:
        with self._cond:
            return list(self._since_unlocked(cursor))

    def wait_since(self, cursor: int, timeout: float) -> list:
        deadline = time.time() + max(0.0, float(timeout))
        with self._cond:
            while True:
                events = self._since_unlocked(cursor)
                if events:
                    return list(events)
                remaining = deadline - time.time()
                if remaining <= 0:
                    return []
                self._cond.wait(timeout=remaining)

    def _since_unlocked(self, cursor: int) -> list:
        start = int(cursor or 0)
        events = [event for event in self._events if event["cursor"] > start]
        if start < self.cursor and not events:
            return [{
                "cursor": self.cursor,
                "kind": "snapshot",
                "data": {"reason": "replay_unavailable"},
                "ts": time.time(),
            }]
        return events


class LocalModelManager:
    """Single-owner control plane for managed llama.cpp and external endpoints."""

    def __init__(
        self,
        root: Optional[str] = None,
        *,
        catalog: Optional[dict] = None,
        urlopen: Optional[UrlOpen] = None,
        popen: Optional[PopenFactory] = None,
        sleeper: Optional[Callable[[float], None]] = None,
        clock: Optional[Callable[[], float]] = None,
        ready_timeout: float = 90.0,
        probe_transport: Optional[Callable[..., Any]] = None,
    ) -> None:
        self.root = root or state_root()
        os.makedirs(self.root, exist_ok=True)
        self.catalog = catalog if catalog is not None else cached_catalog()
        self.urlopen = urlopen or _default_urlopen
        self.popen = popen or subprocess.Popen
        self.sleep = sleeper or time.sleep
        self.clock = clock or time.time
        self.ready_timeout = ready_timeout
        self.probe_transport = probe_transport
        seeded = 0
        try:
            seeded = max(0, int((load_state(self.root) or {}).get("event_cursor") or 0))
        except Exception:
            seeded = 0
        self.events = EventLog(cursor=seeded)
        self._lock = threading.RLock()
        self._cancel = {key: threading.Event() for key in ("runtime", "model", "all")}
        self._workers = {}
        self._procs = {}
        self._start_in_progress = False
        self._install_in_progress = False
        self._shutdown = False
        if seeded > 0:
            self._emit("snapshot", {"reason": "replay_unavailable"})

    # -- paths -------------------------------------------------------------

    def runtime_dir(self) -> str:
        return os.path.join(self.root, "runtime")

    def models_dir(self) -> str:
        return os.path.join(self.root, "models")

    def downloads_dir(self) -> str:
        return os.path.join(self.root, "downloads")

    def logs_dir(self) -> str:
        return os.path.join(self.root, "logs")

    def _state(self) -> dict:
        with self._lock:
            return load_state(self.root)

    def _save(self, state: dict) -> dict:
        with self._lock:
            state["event_cursor"] = self.events.cursor
            return save_state(state, self.root)

    def _update_state(self, mutator: Optional[Callable[[dict], Any]] = None) -> dict:
        """Load, mutate, and persist under one lock hold. *mutator* may be None."""
        with self._lock:
            state = load_state(self.root)
            if mutator is not None:
                mutator(state)
            state["event_cursor"] = self.events.cursor
            return save_state(state, self.root)

    def _emit(self, kind: str, data: Optional[dict] = None) -> None:
        with self._lock:
            event = self.events.append(kind, data)
            state = load_state(self.root)
            state["event_cursor"] = event["cursor"]
            save_state(state, self.root)

    def snapshot(self) -> dict:
        with self._lock:
            state = load_state(self.root)
            hardware = detect_hardware(self.root, self.catalog)
            return snapshot_from_state(
                state,
                catalog=self.catalog,
                hardware=hardware,
                events=self.events.since(max(0, int(state.get("event_cursor") or 0) - 32)),
            )

    def events_since(self, cursor: int) -> list:
        return self.events.since(cursor)

    def wait_events_since(self, cursor: int, timeout: float) -> list:
        return self.events.wait_since(cursor, timeout)

    # -- downloads ---------------------------------------------------------

    def _part_path(self, filename: str) -> str:
        os.makedirs(self.downloads_dir(), exist_ok=True)
        return os.path.join(self.downloads_dir(), filename + ".part")

    def _final_path(self, filename: str) -> str:
        os.makedirs(self.downloads_dir(), exist_ok=True)
        return os.path.join(self.downloads_dir(), filename)

    def cancel(self, target: str = "all") -> dict:
        with self._lock:
            return self._cancel_unlocked(target)

    def _cancel_unlocked(self, target: str = "all") -> dict:
        if target == "all":
            self._cancel["all"].set()
        keys = ("runtime", "model") if target == "all" else (target,)
        for key in keys:
            self._cancel.setdefault(key, threading.Event()).set()
            status = ((self._state().get("managed") or {}).get(key) or {}).get("status")
            if status in {"downloading", "extracting"}:
                self._set_component(key, status="paused", error=None)
                downloads = (self._state().get("managed") or {}).get("downloads") or {}
                row = downloads.get(key) if isinstance(downloads.get(key), dict) else {}
                part = str((row or {}).get("part_path") or "")
                if part and os.path.exists(part):
                    updated = dict(row)
                    updated["bytes"] = os.path.getsize(part)
                    updated["phase"] = row.get("phase") or "download"
                    self._set_download(key, updated)
        self._emit("cancelled", {"target": target})
        return self.snapshot()

    def _clear_cancel(self, target: str) -> None:
        if target == "all":
            for key in ("runtime", "model", "all"):
                self._cancel.setdefault(key, threading.Event()).clear()
            return
        self._cancel.setdefault(target, threading.Event()).clear()
        self._cancel["all"].clear()

    def _cancelled(self, target: str) -> bool:
        return self._cancel.get(target, threading.Event()).is_set() or self._cancel["all"].is_set()

    def download_asset(self, asset: dict, *, target: str) -> str:
        filename = asset["filename"]
        url = asset["url"]
        expected = str(asset["sha256"]).lower()
        total = int(asset.get("size") or 0)
        if total <= 0:
            raise LocalModelError("Catalog asset is missing a pinned size", code="download")
        part = self._part_path(filename)
        final = self._final_path(filename)
        if os.path.exists(final) and os.path.getsize(final) == total:
            if sha256_file(final) == expected:
                self._set_download(target, {
                    "filename": filename,
                    "bytes": total,
                    "total": total,
                    "path": final,
                    "phase": "verified",
                })
                return final
            try:
                os.remove(final)
            except OSError:
                pass
        existing = os.path.getsize(part) if os.path.exists(part) else 0
        if existing == total:
            if sha256_file(part) == expected:
                os.replace(part, final)
                self._set_download(target, {
                    "filename": filename,
                    "bytes": total,
                    "total": total,
                    "path": final,
                    "phase": "verified",
                })
                return final
            try:
                os.remove(part)
            except OSError:
                pass
            existing = 0
        elif existing <= 0 or existing >= total:
            if existing:
                try:
                    os.remove(part)
                except OSError:
                    pass
            existing = 0
        if self._cancelled(target):
            raise LocalModelError("Download cancelled", code="cancelled")
        headers = {"User-Agent": "marionette-local-models"}
        ranged = existing > 0
        if ranged:
            headers["Range"] = "bytes=%s-" % existing
        req = urllib.request.Request(url, headers=headers)
        self._emit("progress", {
            "target": target,
            "filename": filename,
            "bytes": existing,
            "total": total,
            "phase": "download",
        })
        try:
            resp = self.urlopen(req, timeout=60)
        except LocalModelError:
            raise
        except Exception as exc:
            raise LocalModelError("Download failed: %s" % exc, code="download") from exc
        try:
            status = _response_status(resp)
            if ranged:
                if status == 200:
                    existing = 0
                    mode = "wb"
                elif status == 206:
                    parsed = parse_content_range(_response_header(resp, "Content-Range"))
                    if (
                        parsed is None
                        or parsed[0] != existing
                        or parsed[2] != total
                        or parsed[1] != total - 1
                    ):
                        raise LocalModelError(
                            "Resume Content-Range does not match the pinned size",
                            code="download",
                        )
                    mode = "ab"
                else:
                    raise LocalModelError(
                        "Unexpected HTTP status for ranged download",
                        code="download",
                    )
            else:
                if status != 200:
                    raise LocalModelError(
                        "Unexpected HTTP status for download",
                        code="download",
                    )
                mode = "wb"
            written = existing
            last_persist_bytes = existing
            last_persist_at = self.clock()
            with open(part, mode) as handle:
                while True:
                    if self._cancelled(target):
                        self._set_download(target, {
                            "filename": filename,
                            "bytes": written,
                            "total": total,
                            "part_path": part,
                            "phase": "download",
                        })
                        raise LocalModelError("Download cancelled", code="cancelled")
                    chunk = resp.read(64 * 1024)
                    if not chunk:
                        break
                    if written + len(chunk) > total:
                        handle.close()
                        try:
                            os.remove(part)
                        except OSError:
                            pass
                        raise LocalModelError(
                            "Download exceeded the pinned size for %s" % filename,
                            code="download",
                        )
                    handle.write(chunk)
                    written = handle.tell()
                    now = self.clock()
                    if (
                        written - last_persist_bytes >= DOWNLOAD_PROGRESS_BYTES
                        or now - last_persist_at >= DOWNLOAD_PROGRESS_INTERVAL
                        or written == total
                    ):
                        self._set_download(target, {
                            "filename": filename,
                            "bytes": written,
                            "total": total,
                            "part_path": part,
                            "phase": "download",
                        })
                        self._emit("progress", {
                            "target": target,
                            "filename": filename,
                            "bytes": written,
                            "total": total,
                            "phase": "download",
                        })
                        last_persist_bytes = written
                        last_persist_at = now
            existing = written
        finally:
            try:
                resp.close()
            except Exception:
                pass
        if existing != total:
            if existing <= 0 or existing >= total:
                try:
                    os.remove(part)
                except OSError:
                    pass
            raise LocalModelError(
                "Download size does not match the pinned size for %s" % filename,
                code="download",
            )
        self._emit("progress", {
            "target": target, "filename": filename, "bytes": existing,
            "total": total, "phase": "hash",
        })
        digest = sha256_file(part)
        if digest != expected:
            try:
                os.remove(part)
            except OSError:
                pass
            raise LocalModelError(
                "SHA-256 mismatch for %s" % filename,
                code="hash_mismatch",
            )
        os.replace(part, final)
        self._set_download(target, {
            "filename": filename,
            "bytes": os.path.getsize(final),
            "total": total,
            "path": final,
            "phase": "verified",
        })
        return final

    def _set_download(self, target: str, payload: dict) -> None:
        with self._lock:
            state = self._state()
            downloads = state["managed"].setdefault("downloads", {})
            downloads[target] = payload
            self._save(state)

    def _set_component(self, kind: str, **fields: Any) -> None:
        with self._lock:
            state = self._state()
            state["managed"].setdefault(kind, {}).update(fields)
            self._save(state)

    def install(self, target: str = "all", *, model_id: str = "", background: bool = True) -> dict:
        chosen = ""
        if target in ("model", "all"):
            chosen = str(model_id or "").strip()
            if not chosen:
                raise LocalModelError("Install requires an explicit model_id", code="model_id")
            if not curated_model(self.catalog, chosen):
                raise LocalModelError("Unknown catalog model", code="unknown_model")
        hardware = detect_hardware(self.root, self.catalog)
        if not hardware.get("supported"):
            raise LocalModelError(
                hardware.get("unsupported_reason") or "This machine cannot host a catalog model",
                code="unsupported",
            )
        overlapping = ("runtime", "model", "all") if target == "all" else (target, "all")
        with self._lock:
            for key in overlapping:
                worker = self._workers.get(key)
                if worker is not None and worker.is_alive():
                    raise LocalModelError("An install is already running", code="busy")
            if self._start_in_progress or self._install_in_progress:
                raise LocalModelError(
                    "An install or server start is already running",
                    code="busy",
                )
            self._clear_cancel(target)
            if background:
                worker = threading.Thread(
                    target=self._install_worker,
                    args=(target, chosen),
                    daemon=True,
                )
                self._workers[target] = worker
                worker.start()
                return self.snapshot()
            self._install_in_progress = True
        try:
            self._install_worker(target, chosen)
            return self.snapshot()
        finally:
            with self._lock:
                self._install_in_progress = False

    def _install_worker(self, target: str, model_id: str = "") -> None:
        failed = None
        try:
            if target in ("runtime", "all"):
                failed = "runtime"
                self._install_runtime()
                failed = None
            if target in ("model", "all"):
                failed = "model"
                self._install_model(model_id)
                failed = None
            self._emit("ready", {"target": target})
        except LocalModelError as exc:
            kind = failed or ("model" if target == "model" else "runtime")
            status = "paused" if exc.code == "cancelled" else "error"
            if exc.code != "cancelled":
                self._emit("error", {"target": kind, "error": str(exc), "code": exc.code})
            else:
                self._emit("paused", {"target": kind})
            if kind in ("runtime", "model"):
                self._set_component(
                    kind,
                    status=status,
                    error=None if exc.code == "cancelled" else str(exc),
                )
        except Exception as exc:
            kind = failed or target
            self._emit("error", {"target": kind, "error": str(exc), "code": "error"})
            if kind in ("runtime", "model"):
                self._set_component(kind, status="error", error=str(exc))

    def _install_runtime(self) -> None:
        current = (self._state().get("managed") or {}).get("runtime") or {}
        if current.get("status") == "ready" and current.get("path") and os.path.isfile(str(current["path"])):
            return
        if self._cancelled("runtime"):
            raise LocalModelError("Download cancelled", code="cancelled")
        asset = runtime_asset_for_platform(self.catalog)
        if not asset:
            raise LocalModelError("No runtime asset for this platform", code="unsupported")
        self._set_component(
            "runtime",
            status="downloading",
            platform=asset["platform"],
            release=asset.get("release") or "",
            error=None,
        )
        archive = self.download_asset(asset, target="runtime")
        dest = os.path.join(self.runtime_dir(), asset.get("release") or "current")
        self._set_component("runtime", status="extracting")
        extract_archive(archive, dest)
        binary = find_binary(dest, asset.get("binary") or "llama-server")
        if not binary:
            raise LocalModelError("llama-server was not found in the archive", code="extract")
        self._set_component(
            "runtime",
            status="ready",
            path=binary,
            sha256=asset["sha256"],
            error=None,
        )
        self._emit("runtime_ready", {"path": binary})

    def _install_model(self, model_id: str = "") -> None:
        model = curated_model(self.catalog, model_id or None)
        if not model:
            raise LocalModelError("Unknown catalog model", code="unknown_model")
        current = (self._state().get("managed") or {}).get("model") or {}
        if (
            current.get("status") == "ready"
            and current.get("id") == model["id"]
            and current.get("path")
            and os.path.isfile(str(current["path"]))
        ):
            return
        if self._cancelled("model"):
            raise LocalModelError("Download cancelled", code="cancelled")
        self._set_component(
            "model",
            status="downloading",
            id=model["id"],
            error=None,
        )
        downloaded = self.download_asset(model, target="model")
        os.makedirs(self.models_dir(), exist_ok=True)
        dest = os.path.join(self.models_dir(), model["filename"])
        os.replace(downloaded, dest)
        self._set_component(
            "model",
            status="ready",
            id=model["id"],
            path=dest,
            sha256=model["sha256"],
            error=None,
        )
        self._emit("model_ready", {"path": dest, "id": model["id"]})

    # -- process -----------------------------------------------------------

    def _managed_paths(self) -> tuple:
        state = self._state()
        runtime = (state.get("managed") or {}).get("runtime") or {}
        model = (state.get("managed") or {}).get("model") or {}
        return str(runtime.get("path") or ""), str(model.get("path") or ""), str(model.get("id") or "")

    def reconcile_process(self) -> dict:
        """Adopt our llama-server or clear stale PID state without killing strangers."""
        with self._lock:
            state = self._state()
            process = (state.get("managed") or {}).get("process")
            if not process or not process.get("pid"):
                return state
            pid = int(process["pid"])
            owned = int(pid) in self._procs
            identity = dict(process)
        matched = owned or process_matches_identity(pid, identity)
        if matched:
            identity["healthy"] = self._probe_health(
                "http://%s:%s/v1" % (identity.get("host") or "127.0.0.1", identity.get("port")),
                required_alias=str(identity.get("alias") or ""),
            )
            with self._lock:
                state = self._state()
                state["managed"]["process"] = identity
                return self._save(state)
        with self._lock:
            state = self._state()
            current = (state.get("managed") or {}).get("process") or {}
            if current.get("pid") != pid:
                return state
            state["managed"]["process"] = None
            self._emit("stale_pid_cleared", {"pid": pid})
            return self._save(state)

    def start(self) -> dict:
        with self._lock:
            if self._start_in_progress or self._install_in_progress:
                raise LocalModelError("Server start is already running", code="busy")
            for worker in self._workers.values():
                if worker is not None and worker.is_alive():
                    raise LocalModelError("An install is already running", code="busy")
            self._start_in_progress = True
        try:
            return self._start_locked()
        finally:
            with self._lock:
                self._start_in_progress = False

    def _start_locked(self) -> dict:
        self.reconcile_process()
        state = self._state()
        process = (state.get("managed") or {}).get("process")
        pid = process.get("pid") if process else None
        owned = bool(pid and int(pid) in self._procs)
        matched = bool(pid and process_matches_identity(int(pid), process))
        if process and pid and (owned or matched):
            if process.get("healthy"):
                return self.snapshot()
            self.stop()
            state = self._state()
            process = (state.get("managed") or {}).get("process")
        exe, model_path, model_id = self._managed_paths()
        if not exe or not os.path.isfile(exe):
            raise LocalModelError("Install the llama.cpp runtime before starting", code="missing_runtime")
        if not model_path or not os.path.isfile(model_path):
            raise LocalModelError("Install a catalog model before starting", code="missing_model")
        host = "127.0.0.1"
        preferred = process.get("port") if process else None
        port = find_free_port(host, preferred)
        nonce = "%08x" % random.SystemRandom().randint(0, 0xFFFFFFFF)
        alias = "marionette-%s" % nonce
        model = curated_model(self.catalog, model_id)
        ctx = int((model or {}).get("context_length") or 8192)
        runtime_meta = (state.get("managed") or {}).get("runtime") or {}
        asset = runtime_asset_for_platform(
            self.catalog, runtime_meta.get("platform") or None,
        )
        ngl = runtime_offload_layers(asset)
        os.makedirs(self.logs_dir(), exist_ok=True)
        log_path = os.path.join(self.logs_dir(), "llama-server.log")
        log_handle = open(log_path, "ab")
        argv = [
            exe,
            "-m", model_path,
            "--host", host,
            "--port", str(port),
            "--jinja",
            "-c", str(ctx),
            "--alias", alias,
            "-ngl", ngl,
        ]
        env = os.environ.copy()
        env["MARIONETTE_LOCAL_NONCE"] = nonce
        try:
            proc = self.popen(
                argv,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                env=env,
                cwd=os.path.dirname(exe) or None,
                **spawn_popen_kwargs(),
            )
        except Exception as exc:
            log_handle.close()
            raise LocalModelError("Failed to start llama-server: %s" % exc, code="spawn") from exc
        identity = {
            "pid": int(getattr(proc, "pid", 0) or 0),
            "port": port,
            "host": host,
            "exe": exe,
            "model_path": model_path,
            "alias": alias,
            "nonce": nonce,
            "started_at": self.clock(),
            "create_time": self.clock(),
            "start_key": read_process_start_key(int(getattr(proc, "pid", 0) or 0)),
            "healthy": False,
            "context_length": ctx,
        }
        self._procs[identity["pid"]] = (proc, log_handle)

        def _store_process(state: dict) -> None:
            state["managed"]["process"] = identity

        def _clear_process(state: dict) -> None:
            state["managed"]["process"] = None

        self._update_state(_store_process)
        if not self._wait_ready(identity, proc):
            self._release_spawn(identity["pid"], process=identity)
            self._update_state(_clear_process)
            raise LocalModelError("llama-server did not become ready", code="not_ready")
        identity["healthy"] = True
        self._update_state(_store_process)
        self._emit("started", {"port": port, "pid": identity["pid"]})
        return self.snapshot()

    def _wait_ready(self, identity: dict, proc: Any = None) -> bool:
        url = "http://%s:%s/v1" % (identity.get("host") or "127.0.0.1", identity.get("port"))
        alias = str(identity.get("alias") or "")
        deadline = self.clock() + self.ready_timeout
        while self.clock() < deadline:
            if proc is not None:
                poll = getattr(proc, "poll", None)
                if callable(poll) and poll() is not None:
                    return False
            if alias and self._probe_health(url, required_alias=alias):
                return True
            self.sleep(0.2)
        return False

    def _probe_health(self, base_url: str, required_alias: str = "") -> bool:
        try:
            result = self._http_json(base_url.rstrip("/") + "/models", timeout=2.0)
            ids = extract_model_ids(result.get("payload"))
            if required_alias:
                return required_alias in ids
            return bool(ids)
        except Exception:
            if required_alias:
                return False
            try:
                parsed = urlparse(base_url)
                health = "%s://%s/health" % (parsed.scheme, parsed.netloc)
                result = self._http_json(health, timeout=2.0)
                payload = result.get("payload")
                if isinstance(payload, dict) and str(payload.get("status") or "").lower() in {"ok", "ready"}:
                    return True
            except Exception:
                return False
        return False

    def _release_spawn(self, pid: int, process: Optional[dict] = None) -> None:
        handle = self._procs.pop(int(pid), None) if pid else None
        proc = log_handle = None
        if handle:
            proc, log_handle = handle
        identity = process or {}
        if handle:
            try:
                stop_process_tree(int(pid), proc, sleeper=self.sleep)
            except Exception:
                pass
        elif pid and process_matches_identity(int(pid), identity):
            try:
                stop_process_tree(int(pid), proc, sleeper=self.sleep)
            except Exception:
                pass
        elif proc is not None:
            try:
                poll = getattr(proc, "poll", None)
                if callable(poll) and poll() is None:
                    proc.kill()
            except Exception:
                pass
        if log_handle is not None:
            try:
                log_handle.close()
            except Exception:
                pass

    def _close_all_logs(self) -> None:
        for pid in list(self._procs):
            handle = self._procs.pop(pid, None)
            if not handle:
                continue
            _proc, log_handle = handle
            try:
                log_handle.close()
            except Exception:
                pass

    def shutdown(self) -> None:
        """Idempotent stop of the owned process tree (atexit / harness exit)."""
        with self._lock:
            if self._shutdown:
                return
            self._shutdown = True
            owned_pids = list(self._procs)
            process = ((load_state(self.root).get("managed") or {}).get("process") or {})
        if owned_pids:
            for pid in owned_pids:
                try:
                    self._release_spawn(int(pid), process=process)
                except Exception:
                    pass
            try:
                def _clear_process(state: dict) -> None:
                    state["managed"]["process"] = None
                self._update_state(_clear_process)
            except Exception:
                pass
        elif process.get("pid"):
            try:
                self.stop()
            except Exception:
                pass
        self._close_all_logs()

    def stop(self) -> dict:
        with self._lock:
            return self._stop_unlocked()

    def _stop_unlocked(self) -> dict:
        state = self._state()
        process = (state.get("managed") or {}).get("process") or {}
        pid = process.get("pid")
        handle = self._procs.get(int(pid)) if pid else None
        if handle:
            proc, log_handle = handle
            self._procs.pop(int(pid), None)
            try:
                stop_process_tree(int(pid), proc, sleeper=self.sleep)
            except Exception:
                pass
            if log_handle is not None:
                try:
                    log_handle.close()
                except Exception:
                    pass
        elif pid and process_matches_identity(int(pid), process):
            try:
                stop_process_tree(int(pid), None, sleeper=self.sleep)
            except Exception:
                pass
        elif pid:
            self._emit("stale_pid_cleared", {"pid": pid, "reason": "stop_unmatched"})
            leftover = self._procs.pop(int(pid), None)
            if leftover:
                _proc, log_handle = leftover
                try:
                    log_handle.close()
                except Exception:
                    pass
        state = self._state()
        state["managed"]["process"] = None
        self._save(state)
        self._emit("stopped", {})
        return self.snapshot()

    def restart(self) -> dict:
        self.stop()
        return self.start()

    def remove(self, target: str = "all", endpoint_id: str = "") -> dict:
        with self._lock:
            return self._remove_unlocked(target, endpoint_id)

    def _remove_unlocked(self, target: str = "all", endpoint_id: str = "") -> dict:
        if endpoint_id:
            state = self._state()
            state["externals"] = [
                item for item in state.get("externals") or [] if item.get("id") != endpoint_id
            ]
            if str(state.get("active_spec") or "").startswith("local:%s/" % endpoint_id):
                state["active_spec"] = ""
            self._save(state)
            try:
                from .keys import clear_api_key
                clear_api_key(local_secret_reach(endpoint_id))
            except Exception:
                pass
            self._emit("removed", {"endpoint_id": endpoint_id})
            return self.snapshot()
        if target in ("runtime", "all"):
            self.stop()
            shutil.rmtree(self.runtime_dir(), ignore_errors=True)
            self._set_component("runtime", status="absent", path="", sha256="", error=None)
        if target in ("model", "all"):
            shutil.rmtree(self.models_dir(), ignore_errors=True)
            self._set_component("model", status="absent", path="", sha256="", error=None)
        if target == "all":
            shutil.rmtree(self.downloads_dir(), ignore_errors=True)
            state = self._state()
            state["managed"]["downloads"] = {}
            if str(state.get("active_spec") or "").startswith("local:%s/" % MANAGED_ENDPOINT_ID):
                state["active_spec"] = ""
            self._save(state)
        self._emit("removed", {"target": target})
        return self.snapshot()

    # -- external endpoints ------------------------------------------------

    def _http_json(
        self,
        url: str,
        *,
        method: str = "GET",
        headers: Optional[dict] = None,
        body: Optional[bytes] = None,
        timeout: float = 8.0,
        api_key: str = "",
        accept_lan: bool = False,
        accept_remote: bool = False,
        pinned_ip: Optional[str] = None,
    ) -> dict:
        req_headers = {"Accept": "application/json", "User-Agent": "marionette-local-models"}
        if api_key:
            req_headers["Authorization"] = "Bearer %s" % api_key
        if body is not None:
            req_headers.setdefault("Content-Type", "application/json")
        if headers:
            req_headers.update(headers)
        if self.probe_transport is not None:
            result = self.probe_transport(
                url, method=method, headers=req_headers, body=body, timeout=timeout,
            )
            if not isinstance(result, dict):
                raise LocalModelError("Endpoint probe failed", code="probe")
            status = int(result.get("status") or 200)
            if status in {401, 403}:
                raise LocalModelError(
                    "This endpoint rejected the API key (HTTP %s)." % status,
                    code="auth",
                    http_status=status,
                )
            if status >= 400 and status not in {404, 405}:
                raise LocalModelError(
                    "Endpoint returned HTTP %s" % status,
                    code="probe",
                    http_status=status,
                )
            return result
        req = urllib.request.Request(url, data=body, headers=req_headers, method=method)
        try:
            with probe_urlopen(
                req,
                timeout=timeout,
                accept_lan=accept_lan,
                accept_remote=accept_remote,
                pinned_ip=pinned_ip,
            ) as resp:
                raw = read_bounded(resp, PROBE_MAX_BYTES)
                header_map = dict(getattr(resp, "headers", {}) or {})
                payload = json.loads(raw.decode("utf-8", "replace") or "null")
                return {"payload": payload, "headers": header_map, "status": getattr(resp, "status", 200)}
        except urllib.error.HTTPError as exc:
            try:
                exc.read(PROBE_MAX_BYTES)
            except Exception:
                pass
            status = int(exc.code)
            if status in {401, 403}:
                raise LocalModelError(
                    "This endpoint rejected the API key (HTTP %s)." % status,
                    code="auth",
                    http_status=status,
                ) from exc
            raise LocalModelError(
                "Endpoint returned HTTP %s" % status,
                code="probe",
                http_status=status,
            ) from exc
        except LocalModelError:
            raise
        except Exception:
            raise LocalModelError("Endpoint probe failed", code="probe") from None

    def probe(
        self,
        url: str,
        *,
        api_key: str = "",
        accept_lan: bool = False,
        accept_remote: bool = False,
    ) -> dict:
        decision = assert_probe_hop_safe(
            url, accept_lan=accept_lan, accept_remote=accept_remote,
        )
        base = decision["normalized"]
        pin = str(decision.get("pinned_ip") or "")
        models_url = base.rstrip("/") + "/models"
        models = []
        context = None
        headers = {}
        payload = None
        try:
            result = self._http_json(
                models_url,
                api_key=api_key,
                accept_lan=accept_lan,
                accept_remote=accept_remote,
                pinned_ip=pin,
            )
            status = int(result.get("status") or 200)
            if status in {401, 403}:
                raise LocalModelError(
                    "This endpoint rejected the API key (HTTP %s)." % status,
                    code="auth",
                    http_status=status,
                )
            if status in {404, 405}:
                payload = None
            else:
                payload = result.get("payload")
                headers = result.get("headers") or {}
                models = extract_model_ids(payload)
                context = extract_context_length(payload)
        except LocalModelError as exc:
            if exc.code == "auth" or exc.http_status in {401, 403}:
                if exc.code != "auth":
                    raise LocalModelError(
                        "This endpoint rejected the API key (HTTP %s)." % exc.http_status,
                        code="auth",
                        http_status=exc.http_status,
                    ) from exc
                raise
            if exc.http_status not in {404, 405}:
                raise
        vendor = detect_vendor_from_probe(base, headers, payload)
        if vendor == "openai-compatible":
            vendor = detect_vendor_from_url(base)
        if context is None:
            try:
                parsed = urlparse(base)
                props = self._http_json(
                    "%s://%s/props" % (parsed.scheme, parsed.netloc),
                    api_key=api_key,
                    accept_lan=accept_lan,
                    accept_remote=accept_remote,
                    pinned_ip=pin,
                )
                context = extract_context_length(props.get("payload"))
            except Exception:
                context = None
        return redact_mapping({
            "ok": True,
            "url": base,
            "vendor": vendor,
            "kind": decision.get("kind"),
            "models": models,
            "context_length": context,
            "requires_lan": bool(decision.get("requires_lan")),
            "requires_remote": bool(decision.get("requires_remote")),
        })

    def save_external(
        self,
        url: str,
        *,
        api_key: str = "",
        accept_lan: bool = False,
        accept_remote: bool = False,
        model: str = "",
        name: str = "",
        context_length: Optional[int] = None,
    ) -> dict:
        probed = self.probe(
            url, api_key=api_key, accept_lan=accept_lan, accept_remote=accept_remote,
        )
        base = probed["url"]
        endpoint_id = endpoint_id_for_url(base, probed.get("vendor") or "")
        selected = str(model or "").strip() or ((probed.get("models") or [""])[0])
        if not selected:
            raise LocalModelError(
                "Enter a model id; this endpoint did not list models",
                code="no_models",
            )
        reach = local_secret_reach(endpoint_id)
        stored_has_key = False
        try:
            from .keys import get_api_key_status
            stored_has_key = bool(get_api_key_status(reach).get("has_key"))
        except Exception:
            stored_has_key = False
        if api_key.strip():
            try:
                from .keys import set_api_key
                set_api_key(reach, api_key.strip())
            except Exception as exc:
                raise LocalModelError("Could not persist the endpoint key", code="secret") from exc
            has_key = True
        else:
            has_key = stored_has_key
        kind = str(probed.get("kind") or "")
        if not kind:
            try:
                kind = str(evaluate_endpoint_url(
                    base, accept_lan=accept_lan, accept_remote=accept_remote,
                ).get("kind") or "")
            except Exception:
                kind = ""
        requires_key = kind == "public"
        record = {
            "id": endpoint_id,
            "name": name or endpoint_id,
            "vendor": probed.get("vendor") or "openai-compatible",
            "base_url": base,
            "models": probed.get("models") or [selected],
            "selected_model": selected,
            "context_length": context_length or probed.get("context_length"),
            "has_key": has_key,
            "lan_accepted": bool(accept_lan),
            "remote_accepted": bool(accept_remote),
            "kind": kind,
            "requires_key": requires_key,
            "last_error": None,
            "healthy": True,
            "tool_calling": empty_tool_calling(),
        }
        with self._lock:
            state = self._state()
            others = [item for item in state.get("externals") or [] if item.get("id") != endpoint_id]
            if not has_key:
                for item in state.get("externals") or []:
                    if item.get("id") == endpoint_id and item.get("has_key"):
                        record["has_key"] = True
                        break
            others.append(record)
            state["externals"] = others
            self._save(state)
        self._emit("external_saved", {"id": endpoint_id, "model": selected})
        return self.snapshot()

    def activate(self, spec: str) -> dict:
        parsed = parse_local_spec(spec)
        if not parsed:
            raise LocalModelError("Spec must look like local:<endpoint>/<model>", code="spec")
        endpoint_id, model = parsed
        state = self.reconcile_process()
        if endpoint_id == MANAGED_ENDPOINT_ID:
            if not ((state.get("managed") or {}).get("process") or {}).get("healthy"):
                self.start()
                state = self._state()
            model_id = ((state.get("managed") or {}).get("model") or {}).get("id") or model
            spec = canonical_spec(MANAGED_ENDPOINT_ID, model_id)
        else:
            found = None
            for item in state.get("externals") or []:
                if item.get("id") == endpoint_id:
                    found = item
                    break
            if not found:
                raise LocalModelError("Unknown external endpoint", code="unknown_endpoint")
            if not found.get("healthy"):
                raise LocalModelError("External endpoint is not healthy", code="unhealthy")
            found["selected_model"] = model or found.get("selected_model")
            spec = canonical_spec(endpoint_id, found["selected_model"])
        with self._lock:
            state = self._state()
            if endpoint_id != MANAGED_ENDPOINT_ID:
                for item in state.get("externals") or []:
                    if item.get("id") == endpoint_id:
                        item["selected_model"] = found["selected_model"]
            state["active_spec"] = spec
            self._save(state)
        try:
            from . import model_visibility as visibility
            curated = visibility.get_enabled()
            if curated:
                visibility.toggle(spec, True)
        except Exception:
            pass
        self._emit("activated", {"spec": spec})
        return self.snapshot()

    def resolve_spec(self, spec: str) -> Optional[dict]:
        return resolve_local_endpoint(self.reconcile_process(), spec)

    def usable_specs(self) -> list:
        return usable_local_specs(self.reconcile_process(), self.catalog)

    def _resolve_saved_api_key(self, record: dict) -> str:
        """Stored secret for *record*. Never returned into state or events.

        Public ``requires_key`` endpoints fail closed with no key. Keyless
        loopback / trusted LAN use the same synthetic bearer as the driver.
        """
        reach = local_secret_reach(str(record.get("id") or ""))
        key = ""
        try:
            from .keys import get_env_var_for_reach, hydrate_reach_key
            hydrate_reach_key(reach)
            env_name = get_env_var_for_reach(reach)
            if env_name:
                key = str(os.environ.get(env_name) or "").strip()
        except Exception:
            key = ""
        if key:
            return key
        if record.get("requires_key"):
            raise LocalModelError(
                "This public endpoint requires an API key.",
                code="requires_key",
            )
        return "local"

    def _record_tool_calling(self, endpoint_id: str, status: str, reason: str) -> dict:
        result = {
            "status": status,
            "reason": bound_plain_reason(reason),
            "checked_at": self.clock(),
        }

        def _store(state: dict) -> None:
            for item in state.get("externals") or []:
                if item.get("id") == endpoint_id:
                    item["tool_calling"] = result

        self._update_state(_store)
        self._emit("tool_calling_checked", {
            "id": endpoint_id,
            "status": result["status"],
            "reason": result["reason"],
        })
        return self.snapshot()

    def verify_tool_calling(self, spec: str) -> dict:
        """Explicit capability check. Does not run during Probe or Save."""
        parsed = parse_local_spec(spec)
        if not parsed:
            raise LocalModelError("Spec must look like local:<endpoint>/<model>", code="spec")
        endpoint_id, model = parsed
        if endpoint_id == MANAGED_ENDPOINT_ID:
            raise LocalModelError(
                "Tool-calling checks apply to saved external endpoints.",
                code="managed",
            )
        state = self.reconcile_process()
        found = None
        for item in state.get("externals") or []:
            if item.get("id") == endpoint_id:
                found = item
                break
        if not found:
            raise LocalModelError("Unknown external endpoint", code="unknown_endpoint")
        if not found.get("healthy"):
            raise LocalModelError("External endpoint is not healthy", code="unhealthy")
        selected = str(model or found.get("selected_model") or "").strip()
        if not selected:
            raise LocalModelError(
                "Enter a model id; this endpoint did not list models",
                code="no_models",
            )
        base = str(found.get("base_url") or "").rstrip("/")
        if not base:
            raise LocalModelError("Unknown external endpoint", code="unknown_endpoint")
        accept_lan = bool(found.get("lan_accepted"))
        accept_remote = bool(found.get("remote_accepted"))
        try:
            api_key = self._resolve_saved_api_key(found)
        except LocalModelError as exc:
            return self._record_tool_calling(
                endpoint_id, "error", tool_calling_error_reason(exc),
            )
        try:
            decision = assert_probe_hop_safe(
                base, accept_lan=accept_lan, accept_remote=accept_remote,
            )
            pin = str(decision.get("pinned_ip") or "")
            result = self._http_json(
                base + "/chat/completions",
                method="POST",
                headers={"Content-Type": "application/json"},
                body=json.dumps(tool_calling_request_body(selected)).encode("utf-8"),
                timeout=20.0,
                api_key=api_key,
                accept_lan=accept_lan,
                accept_remote=accept_remote,
                pinned_ip=pin,
            )
            http_status = int(result.get("status") or 200)
            if http_status in {401, 403}:
                raise LocalModelError(
                    "This endpoint rejected the API key (HTTP %s)." % http_status,
                    code="auth",
                    http_status=http_status,
                )
            if http_status >= 400:
                raise LocalModelError(
                    "Endpoint returned HTTP %s" % http_status,
                    code="probe",
                    http_status=http_status,
                )
            status, reason = classify_tool_calling_payload(result.get("payload"))
        except LocalModelError as exc:
            status, reason = "error", tool_calling_error_reason(exc)
        except Exception:
            status, reason = "error", "The endpoint could not be reached."
        if api_key and api_key != "local" and api_key in reason:
            reason = reason.replace(api_key, "••••")
        return self._record_tool_calling(endpoint_id, status, reason)


_MANAGER = None
_MANAGER_LOCK = threading.Lock()
_ATEXIT_REGISTERED = False


def _atexit_shutdown() -> None:
    mgr = _MANAGER
    if mgr is not None:
        try:
            mgr.shutdown()
        except Exception:
            pass


def get_manager() -> LocalModelManager:
    global _MANAGER, _ATEXIT_REGISTERED
    root = state_root()
    with _MANAGER_LOCK:
        if _MANAGER is None or os.path.normcase(_MANAGER.root) != os.path.normcase(root):
            if _MANAGER is not None:
                try:
                    _MANAGER.shutdown()
                except Exception:
                    pass
            _MANAGER = LocalModelManager(root=root)
            if not _ATEXIT_REGISTERED:
                atexit.register(_atexit_shutdown)
                _ATEXIT_REGISTERED = True
        return _MANAGER


def reset_manager_for_tests() -> None:
    global _MANAGER
    with _MANAGER_LOCK:
        if _MANAGER is not None:
            try:
                _MANAGER.shutdown()
            except Exception:
                pass
        _MANAGER = None


def local_provider_available() -> bool:
    try:
        return bool(get_manager().usable_specs())
    except Exception:
        return False
