"""Durable per-session stream-performance receipts (TTFT / TPS sidecar).

Atomic UTF-8 JSON under ``{state_dir}/stream_performance/{safe_sid}.json``.
Not written into ``_history``, ``_display_transcript``, or the transcript
file. Bounded: keep the newest ``MAX_RECEIPTS_PER_SESSION`` (200) in
chronological order. Corrupt, deep, oversized, or missing files fail soft
to empty; the next successful write repairs the sidecar atomically.

Blank ``state_dir`` fails closed (no tempfile fallback). Symlinked
performance directories, receipt files, and lock files are refused;
resolved paths must stay inside ``state_dir``. Metrics persist only finite
scalars for known keys.

Stdlib only. Thread-safe and cross-process RMW-safe where the OS lock
exists (fcntl / msvcrt) with a thread lock keyed by canonical path.
Path traversal is refused. Sink failures never raise to the chat hot path.
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import stat
import tempfile
import threading
import time
from typing import Any, Dict, Iterator, List, Optional

from .paths import _resolve, path_within
from .stream_performance import (
    BACKEND_READY_TOTAL_MS,
    FIRST_ANSWER_CALLBACK_MS,
    FIRST_CONTENT_CALLBACK_MS,
    FIRST_VISIBLE_ANSWER_MS,
    PRE_REQUEST_PHASE_NAMES,
    PROVIDER_CALL_TOTAL_MS,
    PROVIDER_OUTPUT_TPS,
    STREAM_PERFORMANCE_KEY,
    THROUGHPUT_BASIS,
    THROUGHPUT_BASIS_KEY,
)

RECEIPT_SCHEMA_VERSION = 1
MAX_RECEIPTS_PER_SESSION = 200
RECEIPT_STATUSES = frozenset({"success", "error", "context_overflow"})
PERFORMANCE_SUBDIR = "stream_performance"
_LOAD_MAX_BYTES = 2 * 1024 * 1024
_MAX_LABEL_CHARS = 128
_MAX_JSON_DEPTH = 8
_MAX_NONNEG_INT = 1_000_000_000
_THROUGHPUT_BASIS_ALLOWED = frozenset({THROUGHPUT_BASIS})

_KNOWN_PERF_KEYS = frozenset({
    "content_delta_count",
    FIRST_CONTENT_CALLBACK_MS,
    FIRST_ANSWER_CALLBACK_MS,
    FIRST_VISIBLE_ANSWER_MS,
    PROVIDER_OUTPUT_TPS,
    PROVIDER_CALL_TOTAL_MS,
    BACKEND_READY_TOTAL_MS,
    THROUGHPUT_BASIS_KEY,
    "decode_window_ms",
    "max_inter_delta_ms",
    "pre_request_total_ms",
    *(f"{name}_ms" for name in PRE_REQUEST_PHASE_NAMES),
})

_PATH_LOCKS: Dict[str, threading.Lock] = {}
_PATH_LOCKS_GUARD = threading.Lock()


def safe_session_id(session_id: str) -> str:
    return "".join(c for c in (session_id or "") if c.isalnum() or c in ("-", "_"))


def _stripped_state_dir(state_dir: str) -> str:
    return (state_dir or "").strip()


def performance_dir(state_dir: str) -> str:
    root = _stripped_state_dir(state_dir)
    if not root:
        return ""
    return os.path.abspath(os.path.join(root, PERFORMANCE_SUBDIR))


def receipt_file_path(state_dir: str, session_id: str) -> str:
    """Absolute sidecar path, or ``""`` when the id/path is unsafe."""
    safe = safe_session_id(session_id)
    root = performance_dir(state_dir)
    if not safe or not root:
        return ""
    path = os.path.abspath(os.path.join(root, f"{safe}.json"))
    try:
        if os.path.commonpath([root, path]) != root:
            return ""
    except ValueError:
        return ""
    if os.path.basename(path) != f"{safe}.json":
        return ""
    return path


def _is_symlink(path: str) -> bool:
    try:
        return bool(path) and os.path.islink(path)
    except OSError:
        return False


def _pytest_blocks_live_state(path: str) -> bool:
    """Under pytest, refuse any I/O that would touch ``~/.pmharness``.

    Lexical (abspath) comparison only — no realpath / open of the live tree.
    """
    if "PYTEST_CURRENT_TEST" not in os.environ:
        return False
    if not path:
        return False
    try:
        live = os.path.normcase(os.path.abspath(os.path.expanduser("~/.pmharness")))
        target = os.path.normcase(os.path.abspath(path))
        if not live or not target:
            return True
        return os.path.commonpath([live, target]) == live
    except (ValueError, OSError, TypeError):
        return True


def _confined_under_state(path: str, state_dir: str) -> bool:
    """True when ``path`` realpath-contains beneath ``state_dir``.

    The path itself must not be a symlink. Intermediate directory symlinks
    (a lexical alias for ``state_dir``) are allowed; a symlinked
    ``stream_performance`` directory or receipt file is refused by callers.
    """
    root = _stripped_state_dir(state_dir)
    if not root or not path:
        return False
    if _is_symlink(path):
        return False
    try:
        return path_within(path, root, allow_equal=False)
    except (ValueError, OSError, TypeError):
        return False


def _lock_file_path(receipt_path: str) -> str:
    if not receipt_path:
        return ""
    parent = os.path.dirname(receipt_path)
    if not parent or _is_symlink(parent):
        return ""
    lock_path = receipt_path + ".lock"
    if os.path.basename(lock_path) != os.path.basename(receipt_path) + ".lock":
        return ""
    try:
        if os.path.commonpath([parent, os.path.abspath(lock_path)]) != parent:
            return ""
    except ValueError:
        return ""
    if _is_symlink(lock_path):
        return ""
    return lock_path


def _canonical_lock_key(path: str) -> str:
    parent = os.path.dirname(path)
    name = os.path.basename(path)
    try:
        if parent and os.path.isdir(parent) and not _is_symlink(parent):
            return os.path.normcase(os.path.join(_resolve(parent), name))
    except OSError:
        pass
    return os.path.normcase(os.path.abspath(path))


def _lock_for(path: str) -> threading.Lock:
    with _PATH_LOCKS_GUARD:
        lock = _PATH_LOCKS.get(path)
        if lock is None:
            lock = threading.Lock()
            _PATH_LOCKS[path] = lock
        return lock


def _acquire_os_lock(fd: int) -> str:
    try:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_EX)
        return "fcntl"
    except (ImportError, OSError, AttributeError):
        pass
    try:
        import msvcrt
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            os.write(fd, b"\0")
        except OSError:
            pass
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
        return "msvcrt"
    except (ImportError, OSError, AttributeError):
        return ""


def _release_os_lock(fd: int, kind: str) -> None:
    try:
        if kind == "fcntl":
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_UN)
        elif kind == "msvcrt":
            import msvcrt
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    except Exception:
        return


@contextlib.contextmanager
def _receipt_lock(path: str) -> Iterator[None]:
    """Canonical-path thread lock plus confined cross-process lock when available."""
    if not path:
        yield
        return
    thread_lock = _lock_for(_canonical_lock_key(path))
    thread_lock.acquire()
    fd: Optional[int] = None
    kind = ""
    try:
        lock_path = _lock_file_path(path)
        if lock_path and not _pytest_blocks_live_state(lock_path):
            flags = os.O_RDWR | os.O_CREAT
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            if hasattr(os, "O_CLOEXEC"):
                flags |= os.O_CLOEXEC
            try:
                fd = os.open(lock_path, flags, 0o600)
                if _is_symlink(lock_path):
                    os.close(fd)
                    fd = None
                else:
                    kind = _acquire_os_lock(fd)
            except OSError:
                if fd is not None:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                    fd = None
        yield
    finally:
        if fd is not None:
            _release_os_lock(fd, kind)
            try:
                os.close(fd)
            except OSError:
                pass
        thread_lock.release()


def _prepare_performance_dir(state_dir: str) -> str:
    """Create or accept a non-symlink performance dir confined under ``state_dir``."""
    root = _stripped_state_dir(state_dir)
    if not root:
        return ""
    root_abs = os.path.abspath(root)
    if _pytest_blocks_live_state(root_abs):
        return ""
    perf = os.path.abspath(os.path.join(root_abs, PERFORMANCE_SUBDIR))
    if _is_symlink(perf):
        return ""
    if os.path.lexists(perf):
        if not os.path.isdir(perf) or _is_symlink(perf):
            return ""
    else:
        try:
            os.makedirs(perf, exist_ok=True)
        except OSError:
            return ""
        if _is_symlink(perf) or not os.path.isdir(perf):
            return ""
    if not _confined_under_state(perf, root_abs):
        return ""
    return perf


def _usable_receipt_path(
    state_dir: str,
    session_id: str,
    *,
    create_dir: bool = False,
) -> str:
    path = receipt_file_path(state_dir, session_id)
    if not path:
        return ""
    if _pytest_blocks_live_state(path):
        return ""
    parent = os.path.dirname(path)
    if _is_symlink(parent) or _is_symlink(path):
        return ""
    if create_dir:
        if not _prepare_performance_dir(state_dir):
            return ""
        if _is_symlink(parent) or _is_symlink(path):
            return ""
    if not _confined_under_state(path if not _is_symlink(path) else parent, state_dir):
        return ""
    return path


def _finite_number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        if math.isfinite(number):
            return number
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            number = float(text)
        except ValueError:
            return None
        if math.isfinite(number):
            return number
    return None


def _finite_numeric_scalar(value: Any) -> Optional[float]:
    """Finite int/float only — not bool, numeric strings, or containers."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        if math.isfinite(number):
            return number
    return None


def _bounded_nonneg_int(value: Any) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if 0 <= value <= _MAX_NONNEG_INT:
        return value
    return None


def _safe_int(
    value: Any,
    *,
    default: int = 0,
    minimum: Optional[int] = None,
    maximum: Optional[int] = None,
) -> int:
    if isinstance(value, bool) or value is None:
        return default
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    if minimum is not None and number < minimum:
        return default
    if maximum is not None and number > maximum:
        return default
    return number


def _safe_label(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    text = value.strip()
    if not text or "\n" in text or "\x00" in text:
        return ""
    if len(text) > _MAX_LABEL_CHARS:
        return text[:_MAX_LABEL_CHARS]
    return text


def copy_stream_performance(raw: Any) -> Dict[str, Any]:
    """Copy known finite scalar snapshot keys. Never mutates ``raw``.

    Timing keys accept finite numeric scalars only (not numeric strings,
    bools, or containers). Counts are bounded nonnegative ints.
    ``throughput_basis`` is an allowlisted string.
    """
    if not isinstance(raw, dict):
        return {}
    out: Dict[str, Any] = {}
    for key in _KNOWN_PERF_KEYS:
        if key not in raw:
            continue
        value = raw[key]
        if isinstance(value, (dict, list, tuple, set)):
            continue
        if key == "content_delta_count":
            number = _bounded_nonneg_int(value)
            if number is None:
                continue
            out[key] = number
            continue
        if key == THROUGHPUT_BASIS_KEY:
            if (
                isinstance(value, str)
                and value in _THROUGHPUT_BASIS_ALLOWED
                and len(value) <= _MAX_LABEL_CHARS
                and "\n" not in value
                and "\x00" not in value
            ):
                out[key] = value
            continue
        number = _finite_numeric_scalar(value)
        if number is None:
            continue
        out[key] = number
    return out


def _model_from_driver(driver: str) -> str:
    text = _safe_label(driver)
    if "/" in text:
        return text.rsplit("/", 1)[-1]
    return text


def build_receipt(
    *,
    session_id: str,
    stream_performance: Any = None,
    turn_index: Any = 0,
    user_ordinal: Any = None,
    provider_step: Any = 0,
    provider_attempt: Any = 0,
    driver: Any = "",
    model: Any = "",
    status: Any = "success",
    captured_at: Any = None,
) -> Dict[str, Any]:
    """Fixed JSON-safe receipt. Unknown / unsafe fields are dropped."""
    sid = safe_session_id(str(session_id or ""))
    status_s = str(status or "").strip().lower()
    if status_s not in RECEIPT_STATUSES:
        status_s = "error" if status_s else "success"
    epoch = _finite_number(captured_at)
    if epoch is None:
        epoch = float(time.time())
    driver_s = _safe_label(driver)
    model_s = _safe_label(model) or _model_from_driver(driver_s)
    receipt: Dict[str, Any] = {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "session_id": sid,
        "turn_index": _safe_int(
            turn_index, default=0, minimum=0, maximum=_MAX_NONNEG_INT,
        ),
        "provider_step": _safe_int(
            provider_step, default=0, minimum=0, maximum=_MAX_NONNEG_INT,
        ),
        "provider_attempt": _safe_int(
            provider_attempt, default=0, minimum=0, maximum=_MAX_NONNEG_INT,
        ),
        "driver": driver_s,
        "model": model_s,
        "status": status_s,
        "captured_at": epoch,
        "stream_performance": copy_stream_performance(stream_performance),
    }
    if user_ordinal is not None and not isinstance(user_ordinal, bool):
        try:
            ordinal = int(user_ordinal)
        except (TypeError, ValueError, OverflowError):
            ordinal = None
        if ordinal is not None and 0 <= ordinal <= _MAX_NONNEG_INT:
            receipt["user_ordinal"] = ordinal
    return receipt


def sanitize_receipt(raw: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(raw, dict):
        return None
    sid = safe_session_id(str(raw.get("session_id") or ""))
    if not sid:
        return None
    return build_receipt(
        session_id=sid,
        stream_performance=raw.get("stream_performance"),
        turn_index=raw.get("turn_index", 0),
        user_ordinal=raw.get("user_ordinal"),
        provider_step=raw.get("provider_step", 0),
        provider_attempt=raw.get("provider_attempt", 0),
        driver=raw.get("driver", ""),
        model=raw.get("model", ""),
        status=raw.get("status", "success"),
        captured_at=raw.get("captured_at"),
    )


def _exceeds_json_depth(value: Any, *, depth: int = 0) -> bool:
    if depth > _MAX_JSON_DEPTH:
        return True
    if isinstance(value, dict):
        return any(_exceeds_json_depth(item, depth=depth + 1) for item in value.values())
    if isinstance(value, list):
        return any(_exceeds_json_depth(item, depth=depth + 1) for item in value)
    return False


def _read_nofollow_capped(path: str, max_bytes: int) -> Optional[bytes]:
    """Read at most ``max_bytes + 1`` from a non-symlink regular file."""
    try:
        info = os.lstat(path)
    except OSError:
        return None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        return None
    if info.st_size > max_bytes:
        return None
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    try:
        fd = os.open(path, flags)
    except OSError:
        return None
    try:
        try:
            opened = os.fstat(fd)
        except OSError:
            return None
        if stat.S_ISLNK(opened.st_mode) or not stat.S_ISREG(opened.st_mode):
            return None
        chunks: List[bytes] = []
        remaining = max_bytes + 1
        while remaining > 0:
            chunk = os.read(fd, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def _atomic_write_json(path: str, data: Dict[str, Any]) -> None:
    if not path or _pytest_blocks_live_state(path):
        return
    parent = os.path.dirname(path)
    if not parent or _is_symlink(parent) or _is_symlink(path):
        return
    os.makedirs(parent, exist_ok=True)
    if _is_symlink(parent) or not os.path.isdir(parent):
        return
    fd, tmp = tempfile.mkstemp(prefix="stream-perf-", suffix=".json", dir=parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2, allow_nan=False)
        if _is_symlink(path) or _is_symlink(parent):
            raise OSError("refusing to replace a symlinked receipt path")
        os.replace(tmp, path)
    except Exception:
        try:
            if os.path.lexists(tmp):
                os.unlink(tmp)
        except OSError:
            pass
        raise


def _load_document(path: str) -> List[Dict[str, Any]]:
    if not path or _pytest_blocks_live_state(path):
        return []
    if _is_symlink(path) or _is_symlink(os.path.dirname(path)):
        return []
    raw = _read_nofollow_capped(path, _LOAD_MAX_BYTES)
    if raw is None or len(raw) > _LOAD_MAX_BYTES:
        return []
    try:
        text = raw.decode("utf-8")
        data = json.loads(text)
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        ValueError,
        TypeError,
        RecursionError,
        OverflowError,
        MemoryError,
    ):
        return []
    except Exception:
        return []
    try:
        if _exceeds_json_depth(data):
            return []
    except RecursionError:
        return []
    if not isinstance(data, dict):
        return []
    rows = data.get("receipts")
    if not isinstance(rows, list):
        return []
    out: List[Dict[str, Any]] = []
    for item in rows:
        receipt = sanitize_receipt(item)
        if receipt is not None:
            out.append(receipt)
    return out


def _unlink_regular_file(path: str) -> None:
    if not path or _pytest_blocks_live_state(path) or _is_symlink(path):
        return
    try:
        info = os.lstat(path)
    except OSError:
        return
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        return
    try:
        os.unlink(path)
    except OSError:
        return


class StreamPerformanceReceiptStore:
    """Session-scoped atomic JSON receipt log. One file per harness session."""

    def __init__(
        self,
        state_dir: str,
        *,
        max_receipts: int = MAX_RECEIPTS_PER_SESSION,
    ) -> None:
        raw = _stripped_state_dir(state_dir)
        self.state_dir = os.path.abspath(raw) if raw else ""
        try:
            cap = int(max_receipts)
        except (TypeError, ValueError):
            cap = MAX_RECEIPTS_PER_SESSION
        self.max_receipts = max(1, cap)

    def list_receipts(
        self,
        session_id: str,
        *,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        try:
            path = _usable_receipt_path(self.state_dir, session_id)
            if not path:
                return []
            with _receipt_lock(path):
                rows = _load_document(path)
        except Exception:
            return []
        if limit is None:
            return [dict(row) for row in rows]
        try:
            n = int(limit)
        except (TypeError, ValueError):
            n = self.max_receipts
        if n < 1:
            return []
        return [dict(row) for row in rows[-n:]]

    def record(self, session_id: str, receipt: Dict[str, Any]) -> None:
        try:
            path = _usable_receipt_path(self.state_dir, session_id, create_dir=True)
            if not path:
                return
            cleaned = sanitize_receipt(receipt)
            if cleaned is None:
                return
            cleaned["session_id"] = safe_session_id(session_id) or cleaned["session_id"]
            with _receipt_lock(path):
                if _is_symlink(path) or _is_symlink(os.path.dirname(path)):
                    return
                rows = _load_document(path)
                rows.append(cleaned)
                if len(rows) > self.max_receipts:
                    rows = rows[-self.max_receipts:]
                payload = {
                    "schema_version": RECEIPT_SCHEMA_VERSION,
                    "session_id": cleaned["session_id"],
                    "receipts": rows,
                }
                _atomic_write_json(path, payload)
        except Exception:
            return

    def delete_session(self, session_id: str) -> None:
        try:
            path = _usable_receipt_path(self.state_dir, session_id)
            if not path:
                return
            with _receipt_lock(path):
                _unlink_regular_file(path)
        except Exception:
            return


def remove_session_performance_receipts(state_dir: str, session_id: str) -> None:
    """Best-effort sidecar delete. Never raises."""
    try:
        StreamPerformanceReceiptStore(state_dir).delete_session(session_id)
    except Exception:
        return


__all__ = (
    "MAX_RECEIPTS_PER_SESSION",
    "RECEIPT_SCHEMA_VERSION",
    "RECEIPT_STATUSES",
    "STREAM_PERFORMANCE_KEY",
    "StreamPerformanceReceiptStore",
    "build_receipt",
    "copy_stream_performance",
    "performance_dir",
    "receipt_file_path",
    "remove_session_performance_receipts",
    "safe_session_id",
    "sanitize_receipt",
)
