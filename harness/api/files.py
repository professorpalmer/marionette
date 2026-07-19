"""Files / editor / upload HTTP route bodies (peeled from ``harness.server``).

Tree/read/write/preview/upload JSON (and raw-byte preview) handlers take a
:class:`FileServices` (or ``upload_dir``) so this module never imports
``harness.server`` at top level. ``server.Handler`` keeps auth/token gates
and thin path delegates. Path containment and preview modes are unchanged.
"""

from __future__ import annotations

import os
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class FileServices:
    """Explicit deps for file/editor HTTP handlers (injected by ``server.py``)."""

    cfg: Any
    sessions: Any
    upload_dir: str


# ---------------------------------------------------------------------------
# Path / mime helpers (historical server names re-exported with leading _)
# ---------------------------------------------------------------------------

def resolve_editor_path(repo: str, user_path: str) -> tuple[str, str]:
    """Resolve an editor/API path under ``repo`` → ``(abs_path, rel_posix)``.

    Raises ``ValueError`` with a user-facing message on missing/escaped/.git paths.
    """
    from ..paths import is_git_restricted_path, resolve_workspace_path

    abs_path, rel_posix = resolve_workspace_path(repo, user_path)
    if is_git_restricted_path(rel_posix):
        raise ValueError("Access denied: .git files are restricted")
    return abs_path, rel_posix


def guess_file_mime(path: str) -> str:
    import mimetypes

    mime, _ = mimetypes.guess_type(path)
    return mime or "application/octet-stream"


def sqlite_table_names(path: str) -> list[str] | None:
    """Best-effort read-only table list for .db/.sqlite files; None if not sqlite."""
    ext = os.path.splitext(path)[1].lower()
    if ext not in {".db", ".sqlite", ".sqlite3"}:
        return None
    try:
        import sqlite3

        uri = Path(path).resolve().as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=1.0)
        try:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            ).fetchall()
            return [str(r[0]) for r in rows if r and r[0]]
        finally:
            conn.close()
    except Exception:
        return []


def binary_file_payload(abs_path: str, rel_posix: str) -> dict[str, Any]:
    """Metadata for binary workspace files (editor calm panel, not CodeMirror)."""
    size = 0
    try:
        size = os.path.getsize(abs_path)
    except OSError:
        pass
    name = os.path.basename(abs_path) or rel_posix
    ext = os.path.splitext(name)[1].lower()
    payload: dict[str, Any] = {
        "ok": False,
        "binary": True,
        "error": "Cannot read binary files",
        "path": rel_posix,
        "name": name,
        "size": size,
        "mime": guess_file_mime(abs_path),
        "ext": ext,
    }
    tables = sqlite_table_names(abs_path)
    if tables is not None:
        payload["sqlite_tables"] = tables
    return payload


def parse_multipart_files(body: bytes, content_type: str) -> list:
    """Extract uploaded files from a multipart/form-data body using the stdlib
    email parser. Replaces cgi.FieldStorage, which was removed in Python 3.13.
    Returns a list of (filename, data_bytes) for every part carrying a filename.
    The body is already size-capped by the caller, so buffering it is bounded."""
    from email.parser import BytesParser
    # Synthesize the MIME header block the parser needs, then feed it the body.
    header = (b"MIME-Version: 1.0\r\nContent-Type: "
              + content_type.encode("latin-1", "replace") + b"\r\n\r\n")
    message = BytesParser().parsebytes(header + body)
    files = []
    if not message.is_multipart():
        return files
    for part in message.get_payload():
        filename = part.get_filename()
        if not filename:
            continue
        data = part.get_payload(decode=True)
        if data is None:
            continue
        files.append((filename, data))
    return files


def _mutate_path_error_status(msg: str) -> int:
    return 403 if "Access denied" in msg or "escapes" in msg or ".git" in msg else 400


def _read_path_error_status(msg: str) -> int:
    return 403 if "Access denied" in msg else 400


def _repo_or_error(svc: FileServices) -> tuple[str | None, tuple[int, dict] | None]:
    repo = svc.cfg.repo
    if not repo or not os.path.exists(repo):
        return None, (400, {"error": "No open workspace"})
    return repo, None


# ---------------------------------------------------------------------------
# POST mutators
# ---------------------------------------------------------------------------

def post_file_write(body: dict, svc: FileServices) -> tuple[int, dict]:
    """POST /api/file/write — atomic UTF-8 write under the open workspace."""
    repo, err = _repo_or_error(svc)
    if err is not None:
        return err
    rel_path = body.get("path", "").strip()
    content = body.get("content", "")
    if not rel_path:
        return 400, {"error": "Missing path parameter"}
    try:
        target_path, rel_posix = resolve_editor_path(repo, rel_path)
    except ValueError as e:
        msg = str(e)
        return _mutate_path_error_status(msg), {"error": msg}
    try:
        try:
            from ..checkpoints import CheckpointStore
            active_sid = svc.sessions.active or ""
            store = CheckpointStore(repo, session_id=active_sid or None)
            store.snapshot(
                label=f"before manual edit {rel_posix or rel_path}",
                trigger="manual_edit",
                session_id=active_sid or None,
            )
        except Exception as cp_err:
            import sys
            print(f"Checkpoint error before write: {cp_err}", file=sys.stderr)

        target_dir = os.path.dirname(target_path)
        os.makedirs(target_dir, exist_ok=True)
        fd, temp_path = tempfile.mkstemp(dir=target_dir, prefix=".tmp-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
                f.write(content)
            os.replace(temp_path, target_path)
        except Exception as e:
            if os.path.exists(temp_path):
                os.remove(temp_path)
            raise e
        bytes_written = len(content.encode("utf-8"))
        return 200, {"ok": True, "bytes": bytes_written}
    except Exception as e:
        return 500, {"error": f"Failed to write file: {e}"}


def post_file_delete(body: dict, svc: FileServices) -> tuple[int, dict]:
    """POST /api/file/delete — remove a file or directory under the workspace."""
    repo, err = _repo_or_error(svc)
    if err is not None:
        return err
    rel_path = body.get("path", "").strip()
    if not rel_path:
        return 400, {"error": "Missing path parameter"}
    try:
        target_path, rel_posix = resolve_editor_path(repo, rel_path)
    except ValueError as e:
        msg = str(e)
        return _mutate_path_error_status(msg), {"error": msg}
    # Never delete the workspace root itself.
    repo_abs = os.path.realpath(repo)
    if os.path.realpath(target_path) == repo_abs:
        return 400, {"error": "Cannot delete workspace root"}
    if not os.path.exists(target_path):
        return 404, {"error": "Path not found", "path": rel_posix}
    try:
        import shutil as _shutil
        if os.path.isdir(target_path) and not os.path.islink(target_path):
            _shutil.rmtree(target_path)
        else:
            os.remove(target_path)
        return 200, {"ok": True, "path": rel_posix}
    except Exception as e:
        return 500, {"error": f"Failed to delete: {e}"}


def post_file_rename(body: dict, svc: FileServices) -> tuple[int, dict]:
    """POST /api/file/rename — same-dir rename or from/to move under workspace."""
    repo, err = _repo_or_error(svc)
    if err is not None:
        return err
    # Accept {path, new_name} (same-dir rename) or {from, to} (rel paths).
    src_raw = (body.get("from") or body.get("path") or "").strip()
    new_name = (body.get("new_name") or "").strip()
    dst_raw = (body.get("to") or "").strip()
    if not src_raw:
        return 400, {"error": "Missing path/from parameter"}
    if new_name and dst_raw:
        return 400, {"error": "Provide new_name or to, not both"}
    if not new_name and not dst_raw:
        return 400, {"error": "Missing new_name or to parameter"}
    if new_name:
        # Same-directory rename: refuse path separators in the new name.
        if "/" in new_name or "\\" in new_name or new_name in (".", ".."):
            return 400, {"error": "Invalid new_name"}
    try:
        src_path, src_rel = resolve_editor_path(repo, src_raw)
    except ValueError as e:
        msg = str(e)
        return _mutate_path_error_status(msg), {"error": msg}
    if not os.path.exists(src_path):
        return 404, {"error": "Path not found", "path": src_rel}
    if new_name:
        dst_raw = "/".join(
            p for p in (*(src_rel.split("/")[:-1] if src_rel else ()), new_name) if p
        )
    try:
        dst_path, dst_rel = resolve_editor_path(repo, dst_raw)
    except ValueError as e:
        msg = str(e)
        return _mutate_path_error_status(msg), {"error": msg}
    if os.path.realpath(src_path) == os.path.realpath(repo):
        return 400, {"error": "Cannot rename workspace root"}
    if os.path.exists(dst_path):
        return 409, {"error": "Destination already exists", "path": dst_rel}
    try:
        dst_parent = os.path.dirname(dst_path)
        if dst_parent and not os.path.isdir(dst_parent):
            os.makedirs(dst_parent, exist_ok=True)
        os.rename(src_path, dst_path)
        return 200, {"ok": True, "from": src_rel, "to": dst_rel}
    except Exception as e:
        return 500, {"error": f"Failed to rename: {e}"}


def post_file_mkdir(body: dict, svc: FileServices) -> tuple[int, dict]:
    """POST /api/file/mkdir — create a directory under the open workspace."""
    repo, err = _repo_or_error(svc)
    if err is not None:
        return err
    rel_path = body.get("path", "").strip()
    if not rel_path:
        return 400, {"error": "Missing path parameter"}
    try:
        target_path, rel_posix = resolve_editor_path(repo, rel_path)
    except ValueError as e:
        msg = str(e)
        return _mutate_path_error_status(msg), {"error": msg}
    if os.path.exists(target_path):
        return 409, {"error": "Path already exists", "path": rel_posix}
    try:
        os.makedirs(target_path, exist_ok=False)
        return 200, {"ok": True, "path": rel_posix}
    except Exception as e:
        return 500, {"error": f"Failed to create directory: {e}"}


def post_file_reveal(body: dict, svc: FileServices) -> tuple[int, dict]:
    """POST /api/file/reveal — OS file-manager reveal for a workspace path."""
    # Prefer Electron shell.showItemInFolder when the preload bridge is present;
    # this HTTP path covers stale shells and HTTP-only UIs so the FILES menu
    # never toasts "web build".
    repo, err = _repo_or_error(svc)
    if err is not None:
        return err
    rel_path = body.get("path", "").strip()
    if not rel_path:
        return 400, {"error": "Missing path parameter"}
    try:
        target_path, rel_posix = resolve_editor_path(repo, rel_path)
    except ValueError as e:
        msg = str(e)
        return _mutate_path_error_status(msg), {"error": msg}
    if not os.path.exists(target_path):
        return 404, {"error": "Path not found", "path": rel_posix}
    try:
        from ..file_reveal import reveal_in_file_manager
        err_msg = reveal_in_file_manager(target_path)
        if err_msg:
            return 500, {"error": err_msg, "path": rel_posix}
        return 200, {"ok": True, "path": rel_posix}
    except Exception as e:
        return 500, {"error": f"Failed to reveal: {e}"}


def _parse_multipart_form_data_content_type(
    content_type: str,
) -> tuple[bool, str]:
    """Validate an upload Content-Type is exactly multipart/form-data.

    Returns (ok, boundary_or_reason). We keep this strict to avoid accepting
    arbitrary substring occurrences that don't correspond to real multipart
    bodies.
    """
    if not content_type or not isinstance(content_type, str):
        return False, "missing content type"
    if "\r" in content_type or "\n" in content_type:
        return False, "invalid content type"

    parts = [p.strip() for p in content_type.split(";")]
    media_type = (parts[0] or "").strip().lower()
    if media_type != "multipart/form-data":
        return False, "expected multipart/form-data"

    params: dict[str, str] = {}
    for p in parts[1:]:
        if not p:
            continue
        if "=" not in p:
            continue
        k, v = p.split("=", 1)
        k = k.strip().lower()
        v = v.strip()
        if not k:
            continue
        # RFC 7578: parameter values may be quoted; only double quotes are valid.
        # Reject single-quoted values for multipart parameters.
        if v and v[0] == "'" and v[-1] == "'":
            return False, "single-quoted boundary not permitted"
        if len(v) >= 2 and v[0] == '"' and v[-1] == '"':
            v = v[1:-1]
        params[k] = v

    boundary = params.get("boundary") or ""
    if not boundary:
        return False, "missing boundary"

    # Boundary must be a single token (no whitespace/control chars) so it can
    # be safely used to synthesize a MIME header line.
    if any(ch.isspace() for ch in boundary):
        return False, "invalid boundary"
    if any(ch in boundary for ch in {'"', "'", "\r", "\n"}):
        return False, "invalid boundary"

    # Hard cap: extremely long boundaries can stress parsing.
    if len(boundary) > 200:
        return False, "boundary too long"

    return True, boundary


def check_upload_request(
    content_type: str, content_length: int
) -> tuple[int, dict] | None:
    """Reject bad/oversized uploads before the socket body is read (DoS gate).

    Returns ``(status, error_dict)`` to send immediately, or ``None`` when the
    handler may safely ``rfile.read(content_length)``.
    """
    ok, boundary_or_reason = _parse_multipart_form_data_content_type(content_type)
    if not ok:
        return 400, {"error": str(boundary_or_reason) or "expected multipart/form-data"}
    # Reject oversized bodies BEFORE parsing. Without a ceiling, a large
    # multipart POST is read straight off the socket into memory on a
    # thread-per-request server -- a cheap memory-exhaustion DoS. Cap by the
    # declared Content-Length (default 10MB, env-tunable).
    max_bytes = int(os.environ.get("HARNESS_UPLOAD_MAX_BYTES", str(10 * 1024 * 1024)))
    if content_length <= 0:
        return 400, {"error": "missing or empty body"}
    if content_length > max_bytes:
        return 413, {
            "error": f"upload too large: {content_length} bytes exceeds cap of {max_bytes}"
        }
    return None


def save_upload(body: bytes, content_type: str, upload_dir: str) -> tuple[int, dict]:
    """POST /api/upload body — save image parts under the process upload dir."""
    saved = []
    for filename, data in parse_multipart_files(body, content_type):
        ext = os.path.splitext(filename)[1].lower() or ".png"
        if ext not in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
            continue
        path = os.path.join(upload_dir, f"{uuid.uuid4().hex}{ext}")
        with open(path, "wb") as out:
            out.write(data)
        saved.append({"path": path, "name": filename})
    return 200, {"saved": saved}


# ---------------------------------------------------------------------------
# GET readers / previews / tree
# ---------------------------------------------------------------------------

_RESOLVE_SKIP_DIRS = {
    ".git", ".codegraph", ".venv", "venv", "node_modules", "__pycache__",
    "dist", "build", "release", ".puppetmaster", ".pytest_cache",
}


def get_file_resolve(rel_path: str, svc: FileServices) -> tuple[int, dict]:
    """Resolve a transcript file hint to one unique workspace-confined file.

    Exact paths win. A missing relative path may fall back to a bounded,
    read-only basename/suffix scan. This resolver is deliberately separate
    from every mutation endpoint: writes remain exact and workspace-confined.
    """
    repo, err = _repo_or_error(svc)
    if err is not None:
        return err
    query = (rel_path or "").strip()
    if not query:
        return 400, {"error": "Missing path parameter"}
    try:
        full_path, rel_posix = resolve_editor_path(repo, query)
    except ValueError as e:
        return _read_path_error_status(str(e)), {"error": str(e)}
    if os.path.isfile(full_path):
        return 200, {"ok": True, "path": rel_posix, "exact": True}

    # Never reinterpret an absolute path or traversal-looking hint as a fuzzy
    # lookup. resolve_editor_path already performed containment above.
    drive, _ = os.path.splitdrive(query)
    normalized = query.replace("\\", "/").strip("/")
    if drive or os.path.isabs(query) or not normalized or ".." in normalized.split("/"):
        return 404, {"error": "File not found", "path": rel_posix}

    try:
        max_files = max(1, int(os.environ.get("HARNESS_FILE_RESOLVE_MAX_FILES", "20000")))
    except Exception:
        max_files = 20000
    needle = normalized.casefold()
    basename_only = "/" not in normalized
    matches: list[str] = []
    visited = 0
    for root, dirs, files in os.walk(repo, topdown=True):
        dirs[:] = [
            d for d in dirs
            if d not in _RESOLVE_SKIP_DIRS
        ]
        for filename in files:
            visited += 1
            if visited > max_files:
                return 422, {
                    "error": "File lookup limit reached; use a more specific path",
                    "path": normalized,
                }
            candidate_abs = os.path.join(root, filename)
            candidate_rel = os.path.relpath(candidate_abs, repo).replace("\\", "/")
            folded = candidate_rel.casefold()
            matched = (
                filename.casefold() == needle
                if basename_only
                else folded == needle or folded.endswith("/" + needle)
            )
            if matched:
                matches.append(candidate_rel)
                if len(matches) > 20:
                    break
        if len(matches) > 20:
            break
    matches.sort(key=lambda value: (value.count("/"), value.casefold()))
    if len(matches) == 1:
        return 200, {"ok": True, "path": matches[0], "exact": False}
    if len(matches) > 1:
        return 409, {
            "error": "File path is ambiguous; use a more specific path",
            "path": normalized,
            "candidates": matches[:20],
        }
    return 404, {"error": "File not found", "path": normalized}


def get_file_read(rel_path: str, svc: FileServices) -> tuple[int, dict]:
    """GET /api/file/read — UTF-8 text (or binary metadata) under workspace."""
    repo, err = _repo_or_error(svc)
    if err is not None:
        return err
    rel_path = (rel_path or "").strip()
    if not rel_path:
        return 400, {"error": "Missing path parameter"}
    try:
        full_path, rel_posix = resolve_editor_path(repo, rel_path)
    except ValueError as e:
        msg = str(e)
        return _read_path_error_status(msg), {"error": msg}
    if not os.path.isfile(full_path):
        return 404, {"error": "File not found", "path": rel_posix}
    try:
        with open(full_path, "rb") as f:
            chunk = f.read(1024)
            if b"\x00" in chunk:
                return 200, binary_file_payload(full_path, rel_posix)
    except Exception as e:
        return 500, {"error": f"Failed to check file type: {e}"}
    try:
        file_size = os.path.getsize(full_path)
        truncated = False
        max_bytes = 1024 * 1024
        # Read as bytes then decode: text-mode f.read(n) is characters,
        # so a UTF-8 file over the byte gate could return more than
        # max_bytes of encoded payload (or mis-label truncation).
        with open(full_path, "rb") as f:
            if file_size > max_bytes:
                truncated = True
                raw = f.read(max_bytes)
            else:
                raw = f.read()
        # errors="ignore" drops a trailing partial multibyte sequence
        # when the byte cap splits a character; replace would inject U+FFFD.
        content = raw.decode("utf-8", errors="ignore")
        return 200, {
            "ok": True,
            "path": rel_posix or rel_path,
            "content": content,
            "truncated": truncated,
        }
    except Exception as e:
        return 500, {"error": f"Failed to read file: {e}"}


def get_file_raw(
    rel_path: str, svc: FileServices
) -> tuple[int, bytes | dict, str]:
    """GET /api/file/raw — authenticated bytes for PDF/image/HTML preview.

    Returns ``(status, body_or_error_dict, content_type)``. Error dicts use
    ``application/json``; success uses the guessed/preview MIME.
    """
    # Same path gates as /api/file/read — never an arbitrary-file read outside
    # the workspace.
    repo, err = _repo_or_error(svc)
    if err is not None:
        return err[0], err[1], "application/json"
    rel_path = (rel_path or "").strip()
    if not rel_path:
        return 400, {"error": "Missing path parameter"}, "application/json"
    try:
        full_path, rel_posix = resolve_editor_path(repo, rel_path)
    except ValueError as e:
        msg = str(e)
        return _read_path_error_status(msg), {"error": msg}, "application/json"
    if not os.path.isfile(full_path):
        return 404, {"error": "File not found", "path": rel_posix}, "application/json"
    try:
        size = os.path.getsize(full_path)
        max_bytes = int(os.environ.get("HARNESS_FILE_RAW_MAX_BYTES", str(50 * 1024 * 1024)))
        if size > max_bytes:
            return 413, {"error": "File too large for raw preview"}, "application/json"
        with open(full_path, "rb") as f:
            data = f.read()
    except Exception as e:
        return 500, {"error": f"Failed to read file: {e}"}, "application/json"
    ctype = guess_file_mime(full_path)
    # Browsers sniff HTML; force text/html for .html/.htm so iframe preview works.
    ext = os.path.splitext(full_path)[1].lower()
    if ext in {".html", ".htm"}:
        ctype = "text/html; charset=utf-8"
    elif ext == ".pdf":
        ctype = "application/pdf"
    return 200, data, ctype


def get_image(req_path: str, upload_dir: str) -> tuple[int, bytes | dict, str]:
    """GET /api/image — serve an uploaded image confined to ``upload_dir``."""
    # Serve an uploaded image back to the browser so SENT message
    # thumbnails have a durable src (the composer's blob: preview URL
    # is revoked right after send and never survives a reload). Only
    # ever serve files that live under upload_dir -- this must NOT
    # become an arbitrary-file-read endpoint.
    if not req_path:
        return 400, {"error": "Missing path parameter"}, "application/json"
    upload_real = os.path.realpath(upload_dir)
    file_real = os.path.realpath(req_path)
    try:
        is_under_upload_dir = os.path.commonpath([upload_real, file_real]) == upload_real
    except ValueError:
        # commonpath raises on e.g. mixed drives on Windows -- treat as unsafe.
        is_under_upload_dir = False
    if not is_under_upload_dir:
        return 403, {
            "error": "Access denied: path outside upload directory"
        }, "application/json"
    ext = os.path.splitext(file_real)[1].lower()
    image_ctypes = {
        ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".webp": "image/webp", ".gif": "image/gif",
    }
    if ext not in image_ctypes:
        return 403, {"error": "Access denied: not an image file"}, "application/json"
    if not os.path.isfile(file_real):
        return 404, {"error": "File not found"}, "application/json"
    try:
        size = os.path.getsize(file_real)
        max_bytes = int(os.environ.get("HARNESS_UPLOAD_MAX_BYTES", str(10 * 1024 * 1024)))
        if size > max_bytes:
            return 413, {"error": "Image too large"}, "application/json"
        with open(file_real, "rb") as f:
            data = f.read()
    except Exception as e:
        return 500, {"error": f"Failed to read image: {e}"}, "application/json"
    return 200, data, image_ctypes[ext]


def get_workspace_files(svc: FileServices) -> tuple[int, dict]:
    """GET /api/workspace/files — sorted, capped workspace file tree listing."""
    repo = svc.cfg.repo
    if not repo or not os.path.isdir(repo):
        return 200, {
            "files": [], "truncated": False, "total": 0, "capped": 0,
        }
    files_list = []
    try:
        path_cap = int(os.environ.get("HARNESS_WORKSPACE_FILES_CAP", "2000") or "2000")
    except ValueError:
        path_cap = 2000
    if path_cap < 1:
        path_cap = 2000
    skip_dirs = {
        ".git", "node_modules", ".venv", ".codegraph", "dist", "build",
        ".pytest_cache", "__pycache__", ".mypy_cache", ".ruff_cache", ".idea",
        ".vscode", "venv", ".next", "coverage", ".hermes", "release",
        "backend-dist",
    }
    repo_abs = os.path.abspath(repo)
    for root, dirs, files in os.walk(repo_abs):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        for f in files:
            full_path = os.path.join(root, f)
            rel_path = os.path.relpath(full_path, repo_abs)
            if rel_path == "." or rel_path.startswith(".."):
                continue
            # Forward slashes: the renderer's file tree and @-mention
            # matching expect one separator on every platform.
            files_list.append(rel_path.replace(os.sep, "/"))
    # Collect fully, then sort, then cap — so the kept set is the
    # alphabetical head, not an os.walk-order biased sample.
    files_list.sort()
    total = len(files_list)
    truncated = total > path_cap
    if truncated:
        files_list = files_list[:path_cap]
    return 200, {
        "files": files_list,
        "truncated": truncated,
        "total": total,
        "capped": path_cap if truncated else total,
    }
