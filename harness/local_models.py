"""Local-inference domain: catalog, platform, hardware, endpoints, durable state.

Stdlib-only, Python 3.9, JSON only. The manager is the sole writer; this module
parses and serializes tagged shapes. Generated files live under
``HARNESS_STATE_DIR`` / ``~/.pmharness/local-models``, never the repo.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import platform
import re
import shutil
import threading
from typing import Any, Optional
from urllib.parse import urlparse, urlunparse

from .url_safety import METADATA_HOSTS, METADATA_IPS, sanitize_url_for_display

STATE_VERSION = 1
MANAGED_ENDPOINT_ID = "managed"
LOCAL_PROVIDER = "local"
LOCAL_KEY_ENV = "LOCAL_MODEL_API_KEY"
LLAMA_CPP_KEY_ENV = "LLAMA_CPP_API_KEY"
CATALOG_FILENAME = "local_models_catalog.json"
STATE_FILENAME = "state.json"

COMMAND_TYPES = (
    "probe",
    "save_external",
    "install",
    "cancel",
    "start",
    "stop",
    "restart",
    "remove",
    "activate",
    "verify_tool_calling",
)
TOOL_CALLING_STATUSES = (
    "unverified",
    "verified",
    "unsupported",
    "error",
)
TOOL_CALLING_FUNCTION_NAME = "marionette_capability_probe"
TOOL_CALLING_REASON_LIMIT = 240
INSTALL_TARGETS = ("runtime", "model", "all")
REMOVE_TARGETS = ("model", "runtime", "all")
VENDORS = (
    "llama.cpp",
    "ollama",
    "lmstudio",
    "omlx",
    "vllm",
    "openai-compatible",
)
LOOPBACK_NAMES = {"localhost", "ip6-localhost", "ip6-loopback"}
_SENSITIVE_KEY_RE = re.compile(
    r"(api[_-]?key|authorization|token|secret|password|passwd)",
    re.IGNORECASE,
)
def state_root() -> str:
    """Directory for runtime, models, logs, and state.json."""
    explicit = os.environ.get("HARNESS_STATE_DIR")
    if explicit:
        base = explicit
    else:
        base = os.path.join(os.path.expanduser("~"), ".pmharness")
    root = os.path.join(base, "local-models")
    os.makedirs(root, exist_ok=True)
    return root


def state_path(root: Optional[str] = None) -> str:
    return os.path.join(root or state_root(), STATE_FILENAME)


def catalog_path() -> str:
    return os.path.join(os.path.dirname(__file__), "data", CATALOG_FILENAME)


def empty_state() -> dict:
    return {
        "version": STATE_VERSION,
        "managed": {
            "runtime": {
                "status": "absent",
                "platform": "",
                "release": "",
                "path": "",
                "sha256": "",
                "error": None,
            },
            "model": {
                "status": "absent",
                "id": "",
                "path": "",
                "sha256": "",
                "error": None,
            },
            "process": None,
            "downloads": {},
        },
        "externals": [],
        "active_spec": "",
        "event_cursor": 0,
    }


def _atomic_write_json(path: str, payload: dict) -> None:
    parent = os.path.dirname(path)
    os.makedirs(parent, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def load_state(root: Optional[str] = None) -> dict:
    path = state_path(root)
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            data = json.load(handle)
        if isinstance(data, dict):
            return migrate_state(data)
    except FileNotFoundError:
        pass
    except Exception:
        pass
    return empty_state()


def save_state(data: dict, root: Optional[str] = None) -> dict:
    normalized = migrate_state(data)
    _atomic_write_json(state_path(root), normalized)
    return normalized


def migrate_state(raw: Any) -> dict:
    """Accept older or partial documents and return a v1 state dict."""
    base = empty_state()
    if not isinstance(raw, dict):
        return base
    version = raw.get("version")
    try:
        version = int(version)
    except (TypeError, ValueError):
        version = 0
    if version > STATE_VERSION:
        # Forward-unknown: keep what we understand, do not invent fields.
        version = STATE_VERSION
    managed = raw.get("managed") if isinstance(raw.get("managed"), dict) else {}
    runtime = managed.get("runtime") if isinstance(managed.get("runtime"), dict) else {}
    model = managed.get("model") if isinstance(managed.get("model"), dict) else {}
    downloads = managed.get("downloads") if isinstance(managed.get("downloads"), dict) else {}
    process = managed.get("process") if isinstance(managed.get("process"), dict) else None
    externals = []
    for item in raw.get("externals") or []:
        if isinstance(item, dict) and str(item.get("id") or "").strip():
            externals.append(_normalize_external(item))
    cursor = raw.get("event_cursor")
    try:
        cursor = int(cursor or 0)
    except (TypeError, ValueError):
        cursor = 0
    base["managed"]["runtime"].update({
        "status": str(runtime.get("status") or "absent"),
        "platform": str(runtime.get("platform") or ""),
        "release": str(runtime.get("release") or ""),
        "path": str(runtime.get("path") or ""),
        "sha256": str(runtime.get("sha256") or ""),
        "error": runtime.get("error"),
    })
    base["managed"]["model"].update({
        "status": str(model.get("status") or "absent"),
        "id": str(model.get("id") or ""),
        "path": str(model.get("path") or ""),
        "sha256": str(model.get("sha256") or ""),
        "error": model.get("error"),
    })
    base["managed"]["downloads"] = {
        key: value for key, value in downloads.items() if isinstance(value, dict)
    }
    base["managed"]["process"] = _normalize_process(process) if process else None
    base["externals"] = externals
    base["active_spec"] = str(raw.get("active_spec") or "")
    base["event_cursor"] = max(0, cursor)
    return base


def _normalize_process(raw: dict) -> dict:
    return {
        "pid": int(raw["pid"]) if _is_intlike(raw.get("pid")) else None,
        "port": int(raw["port"]) if _is_intlike(raw.get("port")) else None,
        "host": str(raw.get("host") or "127.0.0.1"),
        "exe": str(raw.get("exe") or ""),
        "model_path": str(raw.get("model_path") or ""),
        "alias": str(raw.get("alias") or ""),
        "nonce": str(raw.get("nonce") or ""),
        "started_at": raw.get("started_at"),
        "create_time": raw.get("create_time"),
        "start_key": str(raw.get("start_key") or ""),
        "healthy": bool(raw.get("healthy")),
        "context_length": int(raw["context_length"]) if _is_intlike(raw.get("context_length")) else None,
    }


def _normalize_external(raw: dict) -> dict:
    models = []
    for item in raw.get("models") or []:
        text = str(item or "").strip()
        if text and text not in models:
            models.append(text)
    return {
        "id": str(raw.get("id") or "").strip(),
        "name": str(raw.get("name") or raw.get("id") or "").strip(),
        "vendor": normalize_vendor(raw.get("vendor")),
        "base_url": str(raw.get("base_url") or "").rstrip("/"),
        "models": models,
        "selected_model": str(raw.get("selected_model") or (models[0] if models else "")),
        "context_length": int(raw["context_length"]) if _is_intlike(raw.get("context_length")) else None,
        "has_key": bool(raw.get("has_key")),
        "lan_accepted": bool(raw.get("lan_accepted")),
        "remote_accepted": bool(raw.get("remote_accepted")),
        "kind": _external_kind(raw),
        "requires_key": _external_requires_key(raw),
        "last_error": raw.get("last_error"),
        "healthy": bool(raw.get("healthy")),
        "tool_calling": normalize_tool_calling(raw.get("tool_calling")),
    }


def empty_tool_calling() -> dict:
    return {"status": "unverified", "reason": "", "checked_at": None}


def bound_plain_reason(value: Any, limit: int = TOOL_CALLING_REASON_LIMIT) -> str:
    text = " ".join(str(value or "").split())
    if len(text) > limit:
        return text[: max(1, limit - 3)].rstrip() + "..."
    return text


def normalize_tool_calling(raw: Any) -> dict:
    base = empty_tool_calling()
    if not isinstance(raw, dict):
        return base
    status = str(raw.get("status") or "").strip().lower()
    if status not in TOOL_CALLING_STATUSES:
        status = "unverified"
    checked = raw.get("checked_at")
    if checked is not None:
        try:
            checked = float(checked)
        except (TypeError, ValueError):
            checked = None
    return {
        "status": status,
        "reason": bound_plain_reason(raw.get("reason") or ""),
        "checked_at": checked,
    }


def tool_calling_request_body(model: str) -> dict:
    """One bounded, non-streaming chat.completions probe. Never executed."""
    return {
        "model": str(model or "").strip(),
        "stream": False,
        "max_tokens": 64,
        "messages": [{
            "role": "user",
            "content": (
                "Call the function %s with ok set to true. "
                "Do not reply with ordinary text."
                % TOOL_CALLING_FUNCTION_NAME
            ),
        }],
        "tools": [{
            "type": "function",
            "function": {
                "name": TOOL_CALLING_FUNCTION_NAME,
                "description": "Report that this model can emit a tool call.",
                "parameters": {
                    "type": "object",
                    "properties": {"ok": {"type": "boolean"}},
                    "required": ["ok"],
                },
            },
        }],
        "tool_choice": {
            "type": "function",
            "function": {"name": TOOL_CALLING_FUNCTION_NAME},
        },
    }


def classify_tool_calling_payload(payload: Any) -> tuple:
    """Map a chat.completions body to verified | unsupported | error.

    Reads ``choices[0].message.tool_calls`` only. ``verified`` requires the
    exact requested ``TOOL_CALLING_FUNCTION_NAME``. Never executes a function
    and does not infer vision, streaming, context, or swarm-worker eligibility.
    """
    if not isinstance(payload, dict):
        return "error", "The endpoint returned a malformed completion payload."
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return "error", "The endpoint returned a completion without choices."
    first = choices[0]
    if not isinstance(first, dict):
        return "error", "The endpoint returned a malformed completion payload."
    message = first.get("message")
    if not isinstance(message, dict):
        return "error", "The endpoint returned a malformed completion payload."
    raw_calls = message.get("tool_calls")
    if raw_calls is not None:
        if not isinstance(raw_calls, list):
            return "error", "The endpoint returned malformed tool_calls."
        names = []
        for item in raw_calls:
            if not isinstance(item, dict):
                return "error", "The endpoint returned malformed tool_calls."
            fn = item.get("function")
            if not isinstance(fn, dict):
                return "error", "The endpoint returned malformed tool_calls."
            name = str(fn.get("name") or "").strip()
            if not name:
                return "error", "The endpoint returned malformed tool_calls."
            names.append(name)
        if names:
            if TOOL_CALLING_FUNCTION_NAME in names:
                return "verified", "This model returned a tool call."
            return "unsupported", "This model returned a different function than requested."
    content = message.get("content")
    if isinstance(content, str) and content.strip():
        return "unsupported", "This model replied with text instead of a tool call."
    if isinstance(content, list) and content:
        return "unsupported", "This model replied with text instead of a tool call."
    return "error", "The endpoint returned a completion without tool_calls."


def tool_calling_error_reason(exc: BaseException) -> str:
    code = str(getattr(exc, "code", "") or "")
    http_status = getattr(exc, "http_status", None)
    try:
        http_status = int(http_status) if http_status is not None else None
    except (TypeError, ValueError):
        http_status = None
    if code == "requires_key":
        return "This public endpoint requires an API key."
    if code == "auth" or http_status in {401, 403}:
        return "The endpoint rejected the API key."
    if code in {"url", "public", "lan", "link_local", "metadata", "blocked", "scheme", "invalid"}:
        return bound_plain_reason(str(exc))
    message = str(exc or "")
    if "malformed" in message.lower():
        return "The endpoint returned a malformed completion payload."
    if http_status in {404, 405}:
        return "The endpoint has no chat completions route."
    if http_status is not None and http_status >= 400:
        return "The endpoint returned HTTP %s." % http_status
    return "The endpoint could not be reached."


def _is_intlike(value: Any) -> bool:
    try:
        int(value)
        return True
    except (TypeError, ValueError):
        return False


def _kind_from_host(host: str) -> str:
    folded = (host or "").lower().rstrip(".")
    kind = _ip_kind(folded)
    if kind == "name":
        if folded.endswith(".local") or folded.endswith(".lan"):
            return "lan"
        if folded in LOOPBACK_NAMES:
            return "loopback"
        return "public"
    return kind


def _external_kind(raw: dict) -> str:
    stored = str(raw.get("kind") or "").strip().lower()
    if stored in {"loopback", "lan", "link_local", "public"}:
        return stored
    try:
        host = (urlparse(str(raw.get("base_url") or "")).hostname or "")
    except ValueError:
        host = ""
    kind = _kind_from_host(host)
    if kind in {"blocked", "metadata", "name", "empty"}:
        return "public"
    return kind


def _external_requires_key(raw: dict) -> bool:
    if "requires_key" in raw:
        return bool(raw.get("requires_key"))
    return _external_kind(raw) == "public"


def load_catalog(path: Optional[str] = None) -> dict:
    catalog_file = path or catalog_path()
    with open(catalog_file, encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError("local models catalog must be an object")
    return parse_catalog(data)


def parse_catalog(raw: dict) -> dict:
    runtime = raw.get("runtime") if isinstance(raw.get("runtime"), dict) else {}
    assets = {}
    raw_assets = runtime.get("assets") if isinstance(runtime.get("assets"), dict) else {}
    for key, item in raw_assets.items():
        if not isinstance(item, dict):
            continue
        sha = str(item.get("sha256") or "").strip().lower()
        if len(sha) != 64 or any(ch not in "0123456789abcdef" for ch in sha):
            raise ValueError("catalog asset %s is missing a SHA-256 digest" % key)
        assets[str(key)] = {
            "filename": str(item.get("filename") or ""),
            "url": str(item.get("url") or ""),
            "sha256": sha,
            "size": int(item.get("size") or 0),
            "archive": str(item.get("archive") or "tar.gz"),
            "backend": infer_runtime_backend(str(key), item),
        }
    models = []
    for item in raw.get("models") or []:
        if not isinstance(item, dict):
            continue
        sha = str(item.get("sha256") or "").strip().lower()
        if len(sha) != 64:
            raise ValueError("catalog model %s is missing a SHA-256 digest" % item.get("id"))
        models.append({
            "id": str(item.get("id") or "").strip(),
            "name": str(item.get("name") or item.get("id") or "").strip(),
            "quant": str(item.get("quant") or ""),
            "filename": str(item.get("filename") or ""),
            "url": str(item.get("url") or ""),
            "revision": str(item.get("revision") or ""),
            "sha256": sha,
            "size": int(item.get("size") or 0),
            "context_length": int(item.get("context_length") or 0),
            "min_ram_gb": float(item.get("min_ram_gb") or 0),
            "min_disk_bytes": int(item.get("min_disk_bytes") or 0),
            "source": str(item.get("source") or ""),
            "trust": str(item.get("trust") or ""),
            "notes": str(item.get("notes") or ""),
        })
    return {
        "version": int(raw.get("version") or 1),
        "runtime": {
            "id": str(runtime.get("id") or "llama.cpp"),
            "vendor": str(runtime.get("vendor") or "ggml-org"),
            "release": str(runtime.get("release") or ""),
            "commit": str(runtime.get("commit") or ""),
            "binary": str(runtime.get("binary") or "llama-server"),
            "assets": assets,
        },
        "models": models,
    }


def infer_runtime_backend(platform_key: str, item: Optional[dict] = None) -> str:
    """Catalog runtime backend: cpu / metal / cuda / vulkan."""
    raw = str((item or {}).get("backend") or "").strip().lower()
    if raw in {"cpu", "metal", "cuda", "vulkan"}:
        return raw
    filename = str((item or {}).get("filename") or "").lower()
    key = str(platform_key or "").lower()
    blob = " ".join((key, filename))
    if "cuda" in blob or "cu12" in blob or "cu11" in blob:
        return "cuda"
    if "vulkan" in blob:
        return "vulkan"
    if "metal" in blob or key.startswith("macos-"):
        return "metal"
    return "cpu"


def runtime_offload_layers(asset: Optional[dict]) -> str:
    """llama-server -ngl value for a catalog runtime asset. CPU assets stay 0."""
    backend = str((asset or {}).get("backend") or "cpu").strip().lower()
    if backend in {"metal", "cuda", "vulkan"}:
        return "99"
    return "0"


def detect_linux_libc(
    *,
    confstr: Optional[Any] = None,
    maps_text: Optional[str] = None,
    lib_names: Optional[list] = None,
) -> str:
    """Return glibc, musl, or unknown. Fail closed when glibc is not proven."""
    getter = confstr if confstr is not None else getattr(os, "confstr", None)
    if callable(getter):
        try:
            value = getter("CS_GNU_LIBC_VERSION")
            if value:
                return "glibc"
        except (ValueError, OSError, AttributeError):
            pass
    text = maps_text
    if text is None:
        try:
            with open("/proc/self/maps", encoding="utf-8", errors="replace") as handle:
                text = handle.read()
        except Exception:
            text = ""
    blob = str(text or "").lower()
    if "ld-musl" in blob or "musl" in blob:
        return "musl"
    names = lib_names
    if names is None:
        names = []
        for folder in ("/lib", "/lib64", "/usr/lib"):
            try:
                names.extend(os.listdir(folder))
            except Exception:
                pass
    for name in names:
        lower = str(name).lower()
        if lower.startswith("ld-musl") or "musl" in lower:
            return "musl"
        if lower.startswith("ld-linux") or lower.startswith("libc-") or lower == "libc.so.6":
            return "glibc"
    return "unknown"


def detect_platform_key(
    system: Optional[str] = None,
    machine: Optional[str] = None,
    libc: Optional[str] = None,
) -> str:
    sys_name = (system or platform.system()).lower()
    arch = (machine or platform.machine()).lower()
    if arch in {"amd64", "x86_64", "x64"}:
        arch = "x64"
    elif arch in {"arm64", "aarch64"}:
        arch = "arm64"
    if sys_name == "darwin":
        return "macos-%s" % arch
    if sys_name.startswith("linux"):
        resolved_libc = libc
        if resolved_libc is None and system is None:
            resolved_libc = detect_linux_libc()
        if resolved_libc and resolved_libc != "glibc":
            return "linux-%s-%s" % (arch, resolved_libc)
        return "linux-%s" % arch
    if sys_name.startswith("win"):
        return "windows-%s" % arch
    return "%s-%s" % (sys_name, arch)


def runtime_asset_for_platform(catalog: dict, platform_key: Optional[str] = None) -> Optional[dict]:
    key = platform_key or detect_platform_key()
    assets = (catalog.get("runtime") or {}).get("assets") or {}
    asset = assets.get(key)
    if not asset:
        return None
    out = dict(asset)
    out["platform"] = key
    out["release"] = (catalog.get("runtime") or {}).get("release") or ""
    out["binary"] = (catalog.get("runtime") or {}).get("binary") or "llama-server"
    return out


def curated_model(catalog: dict, model_id: Optional[str] = None) -> Optional[dict]:
    models = catalog.get("models") or []
    if model_id:
        for item in models:
            if item.get("id") == model_id:
                return item
        return None
    return models[0] if models else None


def _sysctl_int(name: str) -> Optional[int]:
    try:
        import subprocess
        proc = subprocess.run(
            ["sysctl", "-n", name],
            capture_output=True,
            text=True,
            timeout=2,
        )
        if proc.returncode == 0:
            return int((proc.stdout or "").strip())
    except Exception:
        return None
    return None


def detect_ram_bytes() -> Optional[int]:
    if os.name == "posix" and platform.system() == "Darwin":
        value = _sysctl_int("hw.memsize")
        if value:
            return value
    if os.path.exists("/proc/meminfo"):
        try:
            with open("/proc/meminfo", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    if line.startswith("MemTotal:"):
                        parts = line.split()
                        return int(parts[1]) * 1024
        except Exception:
            pass
    if os.name == "nt":
        try:
            import ctypes

            class _MemStatus(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            status = _MemStatus()
            status.dwLength = ctypes.sizeof(_MemStatus)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return int(status.ullTotalPhys)
        except Exception:
            pass
    return None


def detect_accelerator() -> str:
    sys_name = platform.system().lower()
    arch = platform.machine().lower()
    if sys_name == "darwin" and arch in {"arm64", "aarch64"}:
        return "metal"
    nvidia = shutil.which("nvidia-smi")
    if nvidia:
        return "cuda"
    return "cpu"


def detect_hardware(root: Optional[str] = None, catalog: Optional[dict] = None) -> dict:
    platform_key = detect_platform_key()
    ram_bytes = detect_ram_bytes()
    disk_free = None
    try:
        disk_free = int(shutil.disk_usage(root or state_root()).free)
    except Exception:
        disk_free = None
    models = (catalog or {}).get("models") or []
    min_ram = min((float(item.get("min_ram_gb") or 0) for item in models), default=0)
    min_disk = min((int(item.get("min_disk_bytes") or 0) for item in models), default=0)
    ram_gb = (ram_bytes / (1024 ** 3)) if ram_bytes else None
    runtime = runtime_asset_for_platform(catalog, platform_key) if catalog else None
    supported = bool(runtime)
    reason = ""
    if not runtime:
        supported = False
        reason = "No official llama.cpp build is pinned for %s." % platform_key
    elif ram_gb is not None and min_ram and ram_gb < min_ram:
        supported = False
        reason = "This machine has %.1f GB RAM; catalog models need at least %.0f GB." % (
            ram_gb, min_ram,
        )
    elif disk_free is not None and min_disk and disk_free < min_disk:
        supported = False
        reason = "Free disk is below the %.1f GB the catalog install needs." % (
            min_disk / (1024 ** 3),
        )
    return {
        "os": platform.system(),
        "arch": platform.machine(),
        "platform_key": platform_key,
        "ram_bytes": ram_bytes,
        "disk_free_bytes": disk_free,
        "accelerator": detect_accelerator(),
        "supported": supported,
        "unsupported_reason": reason,
        "min_ram_gb": min_ram or None,
        "runtime_available": bool(runtime),
    }


def normalize_vendor(value: Any) -> str:
    text = str(value or "").strip().lower().replace("_", "").replace(" ", "")
    aliases = {
        "llamacpp": "llama.cpp",
        "llama.cpp": "llama.cpp",
        "ollama": "ollama",
        "lmstudio": "lmstudio",
        "lm-studio": "lmstudio",
        "omlx": "omlx",
        "mlx": "omlx",
        "vllm": "vllm",
        "openai": "openai-compatible",
        "openaicompatible": "openai-compatible",
        "openai-compatible": "openai-compatible",
        "generic": "openai-compatible",
        "runpod": "openai-compatible",
    }
    return aliases.get(text, "openai-compatible")


def detect_vendor_from_url(url: str) -> str:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    path = (parsed.path or "").lower()
    port = parsed.port
    if port == 11434 or host.startswith("ollama") or "/api/tags" in path:
        return "ollama"
    if port == 1234 or "lmstudio" in host or "lmstudio" in path:
        return "lmstudio"
    if "omlx" in host or "omlx" in path or "/mlx" in path:
        return "omlx"
    if "vllm" in host or "vllm" in path:
        return "vllm"
    if "runpod" in host:
        return "openai-compatible"
    if port == 8080 or "llama" in host:
        return "llama.cpp"
    return "openai-compatible"


def detect_vendor_from_probe(
    url: str,
    headers: Optional[dict] = None,
    payload: Optional[Any] = None,
) -> str:
    header_blob = " ".join(
        str(value) for value in (headers or {}).values()
    ).lower()
    body_blob = json.dumps(payload).lower() if payload is not None else ""
    combined = header_blob + " " + body_blob
    if "ollama" in combined:
        return "ollama"
    if "lmstudio" in combined or "lm studio" in combined:
        return "lmstudio"
    if "omlx" in combined or "mlx-community" in combined:
        return "omlx"
    if "vllm" in combined:
        return "vllm"
    if "llama.cpp" in combined or "llama-server" in combined:
        return "llama.cpp"
    return detect_vendor_from_url(url)


def _is_metadata_host(host: str) -> bool:
    folded = host.lower().rstrip(".")
    if folded in METADATA_HOSTS or folded in METADATA_IPS:
        return True
    try:
        return str(ipaddress.ip_address(folded)) in METADATA_IPS
    except ValueError:
        return False


def _ip_kind(host: str) -> str:
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        if host.lower().rstrip(".") in LOOPBACK_NAMES:
            return "loopback"
        return "name"
    if ip.is_loopback:
        return "loopback"
    if ip.is_unspecified or ip.is_multicast or ip.is_reserved:
        return "blocked"
    if str(ip) in METADATA_IPS:
        return "metadata"
    if ip.is_link_local:
        return "link_local"
    if ip.is_private:
        return "lan"
    return "public"


def normalize_endpoint_url(url: str) -> str:
    """Normalize localhost to IPv4 and ensure an OpenAI-compat ``/v1`` root."""
    text = (url or "").strip()
    if not text:
        return ""
    if "://" not in text:
        text = "http://" + text
    parsed = urlparse(text)
    host = (parsed.hostname or "").lower().rstrip(".")
    if host in LOOPBACK_NAMES or host == "::1":
        host = "127.0.0.1"
    port = parsed.port
    netloc = host
    if port:
        netloc = "%s:%s" % (host, port)
    path = parsed.path or ""
    if path in ("", "/"):
        path = "/v1"
    elif path.rstrip("/") == "/v1":
        path = "/v1"
    elif not path.rstrip("/").endswith("/v1"):
        # Keep vendor-specific roots (Ollama /api) but default OpenAI-compat to /v1.
        if "/api" not in path:
            path = path.rstrip("/") + "/v1"
    return urlunparse((
        parsed.scheme or "http",
        netloc,
        path.rstrip("/") or "/v1",
        "",
        "",
        "",
    ))


def evaluate_endpoint_url(
    url: str,
    *,
    accept_lan: bool = False,
    accept_remote: bool = False,
) -> dict:
    """Classify a user-supplied inference URL. Never follows redirects."""
    denied = {
        "ok": False,
        "error": "URL is required",
        "normalized": "",
        "kind": "empty",
        "requires_lan": False,
        "requires_remote": False,
    }
    text = (url or "").strip()
    if not text:
        return denied
    if "://" not in text:
        text = "http://" + text
    try:
        parsed = urlparse(text)
    except ValueError:
        denied.update({"error": "URL could not be parsed", "kind": "invalid"})
        return denied
    scheme = (parsed.scheme or "").lower()
    if scheme not in {"http", "https"}:
        denied.update({
            "error": "Only http and https endpoints are allowed",
            "kind": "scheme",
        })
        return denied
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host:
        denied.update({"error": "URL has no host", "kind": "invalid"})
        return denied
    if _is_metadata_host(host):
        denied.update({
            "error": "Cloud metadata endpoints are blocked",
            "kind": "metadata",
        })
        return denied
    kind = _ip_kind(host)
    if kind == "name":
        if host.endswith(".local") or host.endswith(".lan"):
            kind = "lan"
        else:
            kind = "public"
    if kind == "metadata":
        denied.update({
            "error": "Cloud metadata endpoints are blocked",
            "kind": "metadata",
        })
        return denied
    if kind == "blocked":
        denied.update({
            "error": "This host is not a usable local inference target",
            "kind": "blocked",
        })
        return denied
    if kind == "public":
        denied.update({"kind": "public", "requires_remote": True})
        if scheme != "https":
            denied["error"] = "Remote endpoints must use HTTPS"
            return denied
        if not accept_remote:
            denied["error"] = (
                "This is a public remote host. Confirm you trust this HTTPS service."
            )
            return denied
    requires_lan = kind in {"lan", "link_local"}
    if requires_lan and not accept_lan:
        denied.update({
            "error": "This looks like a LAN address. Confirm it is a machine you trust.",
            "kind": kind,
            "requires_lan": True,
        })
        return denied
    normalized = normalize_endpoint_url(text)
    return {
        "ok": True,
        "error": None,
        "normalized": normalized,
        "kind": kind if kind != "name" else "loopback",
        "requires_lan": requires_lan,
        "requires_remote": kind == "public",
        "vendor_guess": detect_vendor_from_url(normalized),
    }


def canonical_spec(endpoint_id: str, model: str) -> str:
    end = str(endpoint_id or "").strip()
    mid = str(model or "").strip()
    if not end or not mid:
        return ""
    return "%s:%s/%s" % (LOCAL_PROVIDER, end, mid)


def parse_local_spec(spec: str) -> Optional[tuple]:
    text = str(spec or "").strip()
    if not text.startswith(LOCAL_PROVIDER + ":"):
        return None
    rest = text[len(LOCAL_PROVIDER) + 1:]
    if "/" not in rest:
        return None
    endpoint_id, model = rest.split("/", 1)
    endpoint_id = endpoint_id.strip()
    model = model.strip()
    if not endpoint_id or not model:
        return None
    return endpoint_id, model


def is_local_spec(spec: str) -> bool:
    return parse_local_spec(spec) is not None


def local_secret_reach(endpoint_id: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_-]+", "-", str(endpoint_id or "").strip()).strip("-_")
    return "local-%s" % (slug or "endpoint")


def _safe_endpoint_slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "-", str(value or "").strip()).strip("-_") or "endpoint"


def endpoint_id_for_url(url: str, vendor: str = "") -> str:
    """Stable ID from vendor plus normalized host/port/path.

    Loopback stays readable (``ollama-127-0-0-1-11434``). Remote hosts that
    share a name but differ by path (RunPod endpoint URLs) get a short
    SHA-256 suffix so they do not collide.
    """
    parsed = urlparse(url or "")
    host = (parsed.hostname or "local").lower().rstrip(".")
    vendor_slug = _safe_endpoint_slug(normalize_vendor(vendor) or "openai-compatible")
    path = (parsed.path or "").rstrip("/") or "/v1"
    port = parsed.port
    kind = _ip_kind(host)
    is_loopback = kind == "loopback" or host in LOOPBACK_NAMES or host in {"127.0.0.1", "::1"}
    if is_loopback:
        host_slug = "127-0-0-1"
        eid = "%s-%s" % (vendor_slug, host_slug)
        if port:
            eid = "%s-%s" % (eid, port)
        return _safe_endpoint_slug(eid)
    identity = "%s\n%s\n%s\n%s" % (vendor_slug, host, port or "", path.lower())
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:10]
    host_slug = host.replace(".", "-")
    return _safe_endpoint_slug("%s-%s-%s" % (vendor_slug, host_slug, digest))


def redact_secret(value: Any) -> str:
    text = str(value or "")
    if not text:
        return ""
    if len(text) <= 8:
        return "••••"
    return "••••" + text[-4:]


def redact_mapping(payload: Any) -> Any:
    """Drop secrets from JSON/SSE/log payloads."""
    if isinstance(payload, dict):
        out = {}
        for key, value in payload.items():
            if _SENSITIVE_KEY_RE.search(str(key)):
                if value:
                    out[key] = redact_secret(value)
                continue
            if str(key) in {"url", "base_url", "log_tail"}:
                out[key] = sanitize_url_for_display(str(value)) if value else value
            else:
                out[key] = redact_mapping(value)
        return out
    if isinstance(payload, list):
        return [redact_mapping(item) for item in payload]
    return payload


def parse_command(body: Any) -> dict:
    if not isinstance(body, dict):
        raise ValueError("command must be a JSON object")
    command_type = str(body.get("type") or body.get("command") or "").strip()
    if command_type not in COMMAND_TYPES:
        raise ValueError("Unknown local-model command")
    command = {"type": command_type}
    if command_type in {"probe", "save_external"}:
        command["url"] = str(body.get("url") or "").strip()
        command["api_key"] = str(body.get("api_key") or "")
        command["accept_lan"] = bool(body.get("accept_lan"))
        command["accept_remote"] = bool(
            body.get("accept_remote") or body.get("trust_remote")
        )
        command["model"] = str(body.get("model") or "").strip()
        command["name"] = str(body.get("name") or "").strip()
        if _is_intlike(body.get("context_length")):
            command["context_length"] = int(body["context_length"])
    elif command_type in {"install", "cancel"}:
        target = str(body.get("target") or "all").strip() or "all"
        if target not in INSTALL_TARGETS:
            raise ValueError("install target must be runtime, model, or all")
        command["target"] = target
        if command_type == "install":
            command["model_id"] = str(body.get("model_id") or "").strip()
    elif command_type == "remove":
        target = str(body.get("target") or "all").strip() or "all"
        command["target"] = target
        command["endpoint_id"] = str(body.get("endpoint_id") or "").strip()
    elif command_type == "activate":
        spec = str(body.get("spec") or "").strip()
        if not spec:
            raise ValueError("activate requires a spec")
        command["spec"] = spec
    elif command_type == "verify_tool_calling":
        spec = str(body.get("spec") or "").strip()
        if not spec:
            raise ValueError("verify_tool_calling requires a spec")
        command["spec"] = spec
    return command


def extract_model_ids(payload: Any) -> list:
    ids = []
    data = payload.get("data") if isinstance(payload, dict) else payload
    if isinstance(payload, dict) and isinstance(payload.get("models"), list):
        data = payload.get("models")
    if not isinstance(data, list):
        return ids
    seen = set()
    for item in data:
        if isinstance(item, dict):
            mid = str(item.get("id") or item.get("name") or "").strip()
        else:
            mid = str(item or "").strip()
        if mid and mid not in seen:
            seen.add(mid)
            ids.append(mid)
    return ids


def extract_context_length(payload: Any) -> Optional[int]:
    if isinstance(payload, dict):
        for key in (
            "context_length",
            "max_model_len",
            "n_ctx_train",
            "n_ctx",
            "default_max_len",
        ):
            if _is_intlike(payload.get(key)) and int(payload[key]) > 0:
                return int(payload[key])
        default = payload.get("default_model_params")
        if isinstance(default, dict) and _is_intlike(default.get("contextLength")):
            return int(default["contextLength"])
        for item in payload.get("data") or []:
            if not isinstance(item, dict):
                continue
            meta = item.get("meta") if isinstance(item.get("meta"), dict) else {}
            for key in ("context_length", "max_model_len", "n_ctx"):
                if _is_intlike(item.get(key)) and int(item[key]) > 0:
                    return int(item[key])
                if _is_intlike(meta.get(key)) and int(meta[key]) > 0:
                    return int(meta[key])
    return None


def managed_usable(state: dict) -> bool:
    managed = (state or {}).get("managed") or {}
    process = managed.get("process") or {}
    runtime_ready = (managed.get("runtime") or {}).get("status") == "ready"
    model_ready = (managed.get("model") or {}).get("status") == "ready"
    return bool(
        runtime_ready
        and model_ready
        and process.get("pid")
        and process.get("healthy")
        and process.get("port")
    )


def usable_local_specs(state: dict, catalog: Optional[dict] = None) -> list:
    specs = []
    managed = (state or {}).get("managed") or {}
    model_id = (managed.get("model") or {}).get("id") or (
        (curated_model(catalog or {}) or {}).get("id") if catalog else ""
    )
    if managed_usable(state) and model_id:
        specs.append(canonical_spec(MANAGED_ENDPOINT_ID, model_id))
    for item in (state or {}).get("externals") or []:
        model = item.get("selected_model") or ""
        if item.get("id") and model and item.get("base_url") and item.get("healthy"):
            specs.append(canonical_spec(item["id"], model))
    return specs


def resolve_local_endpoint(state: dict, spec: str) -> Optional[dict]:
    parsed = parse_local_spec(spec)
    if not parsed:
        return None
    endpoint_id, model = parsed
    if endpoint_id == MANAGED_ENDPOINT_ID:
        if not managed_usable(state):
            return None
        process = ((state.get("managed") or {}).get("process") or {})
        port = process.get("port")
        host = process.get("host") or "127.0.0.1"
        return {
            "endpoint_id": endpoint_id,
            "model": model,
            "base_url": "http://%s:%s/v1" % (host, port),
            "vendor": "llama.cpp",
            "kind": "loopback",
            "requires_key": False,
            "has_key": False,
            "secret_reach": local_secret_reach(endpoint_id),
            "timeout": 600,
        }
    for item in state.get("externals") or []:
        if item.get("id") == endpoint_id:
            if not item.get("healthy"):
                return None
            kind = _external_kind(item)
            requires_key = _external_requires_key(item)
            return {
                "endpoint_id": endpoint_id,
                "model": model or item.get("selected_model") or "",
                "base_url": item.get("base_url") or "",
                "vendor": item.get("vendor") or "openai-compatible",
                "kind": kind,
                "requires_key": requires_key,
                "has_key": bool(item.get("has_key")),
                "secret_reach": local_secret_reach(endpoint_id),
                "timeout": 600,
            }
    return None


def local_provider_is_available(state: Optional[dict] = None) -> bool:
    current = state if state is not None else load_state()
    return bool(usable_local_specs(current))


def local_send_stale_seconds(spec: str) -> float:
    if is_local_spec(spec):
        raw = os.environ.get("HARNESS_SEND_STALE_SECONDS")
        if raw and str(raw).strip():
            try:
                return float(raw)
            except ValueError:
                pass
        return 900.0
    try:
        return float(os.environ.get("HARNESS_SEND_STALE_SECONDS", "180") or 180)
    except ValueError:
        return 180.0


def snapshot_from_state(
    state: dict,
    *,
    catalog: Optional[dict] = None,
    hardware: Optional[dict] = None,
    events: Optional[list] = None,
) -> dict:
    cat = catalog if catalog is not None else load_catalog()
    hw = hardware if hardware is not None else detect_hardware(catalog=cat)
    managed = state.get("managed") or {}
    model = curated_model(cat, (managed.get("model") or {}).get("id") or None)
    runtime = runtime_asset_for_platform(cat, hw.get("platform_key"))
    process = managed.get("process")
    specs = usable_local_specs(state, cat)
    return redact_mapping({
        "hardware": hw,
        "catalog": {
            "runtime_release": (cat.get("runtime") or {}).get("release") or "",
            "runtime": runtime,
            "models": cat.get("models") or [],
            "model": model,
        },
        "managed": {
            "runtime": managed.get("runtime") or {},
            "model": managed.get("model") or {},
            "process": process,
            "downloads": managed.get("downloads") or {},
            "usable": managed_usable(state),
            "spec": canonical_spec(MANAGED_ENDPOINT_ID, (model or {}).get("id") or "") if model else "",
        },
        "externals": state.get("externals") or [],
        "active_spec": state.get("active_spec") or "",
        "usable_specs": specs,
        "event_cursor": int(state.get("event_cursor") or 0),
        "events": events or [],
    })


_catalog_cache = None
_catalog_lock = threading.Lock()


def cached_catalog() -> dict:
    global _catalog_cache
    with _catalog_lock:
        if _catalog_cache is None:
            _catalog_cache = load_catalog()
        return _catalog_cache


def reset_catalog_cache() -> None:
    global _catalog_cache
    with _catalog_lock:
        _catalog_cache = None
