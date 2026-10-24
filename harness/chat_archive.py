from __future__ import annotations

"""Marionette chat archive: Buddy's method, Marionette-owned stores.

Hide (session ``archived``) is not ingest and not prune. Ingest copies an
archived session into ``{state_dir}/chat-archive/archive.sqlite`` plus a
markdown projection and a verified raw JSON backup. Search and read query
that vault. Prune removes the hot
transcript only after a vault copy and backup exist. Unarchive restores
from the vault into the live transcript store — the only write-back.
"""

import hashlib
import json
import os
import re
import sqlite3
import time
import tempfile
from functools import wraps
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

SCHEMA_VERSION = 2
BUSY_TIMEOUT_MS = 5000
PER_CHAT_HITS = 3
MAX_SEARCH = 20
MAX_READ_MESSAGES = 200

TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{2,}")
MAX_TOKENS = 5
STOP = frozenset({
    "about", "after", "also", "and", "any", "are", "because", "been",
    "before", "being", "but", "can", "could", "did", "does", "during",
    "each", "else", "find", "for", "from", "get", "got", "had", "has",
    "have", "help", "how", "into", "its", "just", "know", "let", "like",
    "look", "made", "make", "may", "might", "more", "most", "must",
    "need", "not", "off", "our", "out", "over", "own", "please", "same",
    "see", "should", "some", "such", "than", "that", "the", "their",
    "them", "then", "there", "these", "they", "this", "those", "through",
    "too", "try", "under", "use", "used", "using", "very", "want", "was",
    "were", "what", "when", "where", "which", "who", "why", "will",
    "with", "without", "would", "you", "your",
})

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS chats (
  chat_id TEXT PRIMARY KEY,
  source TEXT NOT NULL,
  origin_id TEXT NOT NULL,
  workspace TEXT,
  name TEXT,
  updated_at INTEGER,
  ingested_at INTEGER NOT NULL,
  content_fp TEXT NOT NULL,
  backup_path TEXT
);
CREATE TABLE IF NOT EXISTS raw_payloads (
  chat_id TEXT PRIMARY KEY, payload TEXT NOT NULL, digest TEXT NOT NULL,
  backup_path TEXT NOT NULL, markdown_digest TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY,
  chat_id TEXT NOT NULL,
  seq INTEGER NOT NULL,
  role TEXT,
  text TEXT NOT NULL,
  UNIQUE (chat_id, seq)
);
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
  text,
  chat_id UNINDEXED,
  name UNINDEXED
);
CREATE INDEX IF NOT EXISTS idx_messages_chat ON messages(chat_id);
CREATE INDEX IF NOT EXISTS idx_chats_source ON chats(source);
"""


@dataclass(frozen=True)
class IngestReport:
    ingested: int = 0
    skipped_unchanged: int = 0
    skipped_missing: int = 0
    errors: int = 0
    vault_present: bool = False
    backup_dir: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "ingested": self.ingested,
            "skipped_unchanged": self.skipped_unchanged,
            "skipped_missing": self.skipped_missing,
            "errors": self.errors,
            "vault_present": self.vault_present,
            "backup_dir": self.backup_dir,
        }


def archive_dir(state_dir: str) -> Path:
    return Path(state_dir) / "chat-archive"


def archive_db_path(state_dir: str) -> Path:
    return archive_dir(state_dir) / "archive.sqlite"


def backup_dir(state_dir: str) -> Path:
    return archive_dir(state_dir) / "backup"


def distinctive_tokens(raw: str) -> Tuple[str, ...]:
    seen = set()
    tokens = []
    for match in TOKEN_RE.findall(raw or ""):
        folded = match.lower()
        if folded in STOP or folded in seen:
            continue
        seen.add(folded)
        tokens.append(folded)
    tokens.sort(key=lambda token: (-len(token), token))
    return tuple(tokens[:MAX_TOKENS])


def _fts_quote(token: str) -> str:
    return '"' + token.replace('"', '""') + '"'


def _fts_or(tokens: Sequence[str]) -> str:
    return " OR ".join(_fts_quote(t) for t in tokens)


def _safe_id(raw: str) -> str:
    cleaned = "".join(c for c in (raw or "") if c.isalnum() or c in ("-", "_", "."))
    return cleaned[:180] or "chat"


def _content_fp(name: str, messages: Sequence[Tuple[str, str]]) -> str:
    h = hashlib.sha256()
    h.update((name or "").encode("utf-8"))
    for role, text in messages:
        h.update(b"\n")
        h.update((role or "").encode("utf-8"))
        h.update(b"\t")
        h.update((text or "").encode("utf-8"))
    return h.hexdigest()


def _connect_archive(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path))
    con.execute("PRAGMA busy_timeout = %d" % BUSY_TIMEOUT_MS)
    con.execute("PRAGMA journal_mode = WAL")
    con.execute("PRAGMA synchronous = FULL")
    con.executescript(SCHEMA)
    con.execute(
        "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(SCHEMA_VERSION),),
    )
    con.commit()
    return con


def _write_backup(state_dir: str, source: str, origin_id: str, name: str, messages: Sequence[Tuple[str, str]]) -> str:
    dest_dir = backup_dir(state_dir) / _safe_id(source)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / ("%s-%s.md" % (_safe_id(origin_id), _content_fp(name, messages)))
    lines = ["# %s" % (name or origin_id), "", "source: %s" % source, "id: %s" % origin_id, ""]
    for role, text in messages:
        heading = (role or "message").strip() or "message"
        lines.extend(["## %s" % heading, "", text or "", ""])
    fd, temporary = tempfile.mkstemp(dir=dest_dir, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, dest)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return str(dest)


def _upsert_chat(
    con: sqlite3.Connection,
    *,
    chat_id: str,
    source: str,
    origin_id: str,
    workspace: str,
    name: str,
    updated_at: int,
    content_fp: str,
    backup_path: str,
    messages: Sequence[Tuple[str, str]],
) -> None:
    now = int(time.time())
    con.execute(
        """
        INSERT INTO chats (
          chat_id, source, origin_id, workspace, name, updated_at,
          ingested_at, content_fp, backup_path
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(chat_id) DO UPDATE SET
          workspace=excluded.workspace,
          name=excluded.name,
          updated_at=excluded.updated_at,
          ingested_at=excluded.ingested_at,
          content_fp=excluded.content_fp,
          backup_path=excluded.backup_path
        """,
        (chat_id, source, origin_id, workspace, name, updated_at, now, content_fp, backup_path),
    )
    old_ids = [
        row[0]
        for row in con.execute("SELECT id FROM messages WHERE chat_id = ?", (chat_id,))
    ]
    for rowid in old_ids:
        try:
            con.execute("DELETE FROM messages_fts WHERE rowid = ?", (rowid,))
        except sqlite3.OperationalError:
            pass
    con.execute("DELETE FROM messages WHERE chat_id = ?", (chat_id,))
    for seq, (role, text) in enumerate(messages):
        cur = con.execute(
            "INSERT INTO messages (chat_id, seq, role, text) VALUES (?, ?, ?, ?)",
            (chat_id, seq, role or "", text or ""),
        )
        try:
            con.execute(
                "INSERT INTO messages_fts (rowid, text, chat_id, name) VALUES (?, ?, ?, ?)",
                (cur.lastrowid, text or "", chat_id, name or ""),
            )
        except sqlite3.OperationalError:
            pass


def _existing_fp(con: sqlite3.Connection, chat_id: str) -> str:
    row = con.execute("SELECT content_fp FROM chats WHERE chat_id = ?", (chat_id,)).fetchone()
    return str(row[0]) if row else ""


def _is_pruned_stub(payload: Any) -> bool:
    return isinstance(payload, dict) and payload.get("pruned") is True


def _session_transcript_path(state_dir: str, session_id: str) -> Path:
    safe = "".join(c for c in (session_id or "") if c.isalnum() or c in ("-", "_"))
    return Path(state_dir) / "transcripts" / ("%s.json" % (safe or "session"))


def _transcript_messages(history: Any) -> List[Tuple[str, str]]:
    from .sessions import _message_plain_text

    if _is_pruned_stub(history):
        return []
    if isinstance(history, dict):
        rows = list(history.get("history") or [])
    elif isinstance(history, list):
        rows = history
    else:
        rows = []
    out: List[Tuple[str, str]] = []
    for msg in rows:
        if not isinstance(msg, dict):
            continue
        text = _message_plain_text(msg)
        if not text:
            continue
        out.append((str(msg.get("role") or ""), text))
    return out


def _serialized_operation(fn):
    @wraps(fn)
    def locked(*args, **kwargs):
        from .native_publication import native_publication
        state_dir = args[0] if args else kwargs['state_dir']
        with native_publication(state_dir):
            return fn(*args, **kwargs)
    return locked


def _raw_transcript(state_dir: str, sid: str) -> Any:
    from .history_compaction_journal import recover_compaction_commit
    path = _session_transcript_path(state_dir, sid)
    recover_compaction_commit(state_dir, path.stem)
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not _is_pruned_stub(raw):
        rows = raw.get("history") if isinstance(raw, dict) else raw
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ValueError("Invalid transcript history shape")
    return raw


def _has_archived_transcript(state_dir: str, sid: str) -> bool:
    """Check recovery provenance without treating database failures as absence."""
    db = archive_db_path(state_dir)
    try:
        db.stat()
    except FileNotFoundError:
        return False
    con = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        chat_id = "marionette:" + sid
        if con.execute("SELECT 1 FROM chats WHERE chat_id=?", (chat_id,)).fetchone():
            return True
        if con.execute("SELECT 1 FROM sqlite_master WHERE name='raw_payloads'").fetchone():
            return con.execute("SELECT 1 FROM raw_payloads WHERE chat_id=?", (chat_id,)).fetchone() is not None
        return False
    finally:
        con.close()


def _bundle(state_dir: str, sid: str, raw: Any) -> Dict[str, Any]:
    from .compaction_archive import compaction_archive_path, verified_archive_digest
    path = Path(compaction_archive_path(state_dir, sid))
    files = {}
    if path.is_file():
        verified_archive_digest(state_dir, sid)
        document = json.loads(path.read_text(encoding="utf-8"))
        files[path.name] = document
        head = document.get("head") if document.get("version") == 2 else ""
        while head:
            segment = path.parent / (path.name + ".segments") / (head + ".json")
            data = json.loads(segment.read_text(encoding="utf-8"))
            files[path.name + ".segments/" + head + ".json"] = data
            head = data.get("parent")
    from .input_receipts import InputReceiptStore
    try:
        inputs = InputReceiptStore(state_dir, sid).snapshot()
    except Exception as exc:
        raise OSError('Input originals cannot be bundled') from exc
    bundle = {"transcript": raw, "compaction_files": files}
    if inputs is not None:
        bundle["inputs"] = inputs
    return bundle


def _verified_raw(state_dir: str, chat_id: str):
    from .compaction_archive import json_digest
    db = archive_db_path(state_dir)
    if not db.is_file():
        return None
    con = sqlite3.connect(str(db))
    try:
        if not con.execute("SELECT 1 FROM sqlite_master WHERE name='raw_payloads'").fetchone():
            return None
        row = con.execute("SELECT payload, digest, backup_path, markdown_digest FROM raw_payloads WHERE chat_id=?", (chat_id,)).fetchone()
        if row is None:
            return None
        data = json.loads(row[0])
        if json_digest(data) != row[1]:
            raise OSError("Archive raw payload digest mismatch")
        backup = json.loads(Path(row[2]).read_text(encoding="utf-8"))
        if json_digest(backup) != row[1]:
            raise OSError("Archive raw backup digest mismatch")
        md = con.execute("SELECT backup_path, name, content_fp FROM chats WHERE chat_id=?", (chat_id,)).fetchone()
        if not md or hashlib.sha256(Path(md[0]).read_bytes()).hexdigest() != row[3]:
            raise OSError("Archive markdown backup digest mismatch")
        projection = con.execute("SELECT role, text FROM messages WHERE chat_id=? ORDER BY seq", (chat_id,)).fetchall()
        if _content_fp(md[1], projection) != md[2]:
            raise OSError("Archive projection fingerprint mismatch")
        return data
    finally:
        con.close()


@_serialized_operation
def ingest_marionette_session(
    state_dir: str,
    session_id: str,
    *,
    title: str = "",
    workspace: str = "",
    updated_at: int = 0,
) -> IngestReport:
    """Copy one session transcript into the archive vault + markdown backup."""
    sid = (session_id or "").strip()
    backups = str(backup_dir(state_dir))
    if not sid:
        return IngestReport(errors=1, backup_dir=backups)
    from .seal import is_sealed
    if is_sealed(sid):
        # The archive vault, its markdown and its raw backup are plaintext.
        return IngestReport(skipped_unchanged=1, backup_dir=backups)
    try:
        raw = _raw_transcript(state_dir, sid)
        if _is_pruned_stub(raw):
            present = _verified_raw(state_dir, "marionette:%s" % sid) is not None
            return IngestReport(skipped_unchanged=int(present), errors=int(not present), vault_present=present, backup_dir=backups)
        bundle = _bundle(state_dir, sid, raw)
    except (OSError, ValueError, sqlite3.Error):
        return IngestReport(errors=1, backup_dir=backups)
    messages = _transcript_messages(raw)
    from .compaction_archive import _atomic_write_json, json_digest, load_compaction_archive_page
    offset = 0
    while bundle["compaction_files"]:
        page, total = load_compaction_archive_page(state_dir, sid, offset=offset, limit=400)
        messages.extend(_transcript_messages(page))
        offset += len(page)
        if offset >= total:
            break
    name = (title or "").strip() or sid
    chat_id = "marionette:%s" % sid
    fp = _content_fp(name, messages)
    dst = _connect_archive(archive_db_path(state_dir))
    try:
        if fp and fp == _existing_fp(dst, chat_id) and _verified_raw(state_dir, chat_id) == bundle:
            return IngestReport(skipped_unchanged=1, vault_present=True, backup_dir=backups)
        backup = _write_backup(state_dir, "marionette", sid, name, messages)
        _upsert_chat(
            dst,
            chat_id=chat_id,
            source="marionette",
            origin_id=sid,
            workspace=workspace or "",
            name=name,
            updated_at=int(updated_at or time.time()),
            content_fp=fp,
            backup_path=backup,
            messages=messages,
        )
        raw_backup = str(Path(backup).with_name(json_digest(bundle) + ".raw.json"))
        _atomic_write_json(raw_backup, bundle)
        dst.execute(
            "INSERT OR REPLACE INTO raw_payloads VALUES (?, ?, ?, ?, ?)",
            (chat_id, json.dumps(bundle), json_digest(bundle), raw_backup,
             hashlib.sha256(Path(backup).read_bytes()).hexdigest()),
        )
        dst.commit()
    finally:
        dst.close()
    return IngestReport(ingested=1, vault_present=True, backup_dir=backups)


def ingest_all(
    state_dir: str,
    *,
    sessions: Optional[Iterable[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Ingest currently archived sessions. Skip unchanged. Leave hot transcripts."""
    ingested = 0
    skipped = 0
    errors = 0
    for row in sessions or ():
        if not row.get("archived"):
            continue
        sid = str(row.get("id") or "")
        if not sid:
            continue
        report = ingest_marionette_session(
            state_dir,
            sid,
            title=str(row.get("title") or ""),
            workspace=str(row.get("workspace_root") or row.get("repo") or ""),
            updated_at=int(row.get("created") or 0),
        )
        ingested += report.ingested
        skipped += report.skipped_unchanged
        errors += report.errors
    return {
        "ok": True,
        "ingested": ingested,
        "skipped_unchanged": skipped,
        "errors": errors,
        "backup_dir": str(backup_dir(state_dir)),
        "archive_db": str(archive_db_path(state_dir)),
    }


@_serialized_operation
def prune_ingested_transcripts(state_dir: str, sessions=None) -> Dict[str, Any]:
    """Prune archived transcripts only after verifying both complete copies."""
    from .compaction_archive import _atomic_write_json
    pruned = skipped = 0
    for row in sessions or ():
        sid = str(row.get("id") or "")
        if not row.get("archived") or not sid:
            continue
        from .seal import is_sealed
        if is_sealed(sid):
            skipped += 1
            continue
        try:
            raw = _raw_transcript(state_dir, sid)
            if _is_pruned_stub(raw):
                skipped += 1
                continue
            saved = _verified_raw(state_dir, "marionette:" + sid)
            if saved is None or saved != _bundle(state_dir, sid, raw):
                skipped += 1
                continue
            _atomic_write_json(str(_session_transcript_path(state_dir, sid)),
                               {"pruned": True, "chat_id": "marionette:" + sid})
            pruned += 1
        except (OSError, ValueError, sqlite3.Error):
            skipped += 1
    return {"ok": True, "pruned": pruned, "skipped": skipped,
            "archive_db": str(archive_db_path(state_dir))}


@_serialized_operation
def restore_pruned_transcript(state_dir: str, session_id: str) -> bool:
    """Restore complete native state, or an uncapped legacy text projection."""
    from .compaction_archive import _atomic_write_json
    sid = (session_id or "").strip()
    if not sid:
        return False
    path = _session_transcript_path(state_dir, sid)
    try:
        if path.exists() and not _is_pruned_stub(_raw_transcript(state_dir, sid)):
            return False
        bundle = _verified_raw(state_dir, "marionette:" + sid)
        if bundle is not None:
            # Publish sidecars before the transcript; retry is safe after interruption.
            for relative, data in sorted(bundle["compaction_files"].items(), reverse=True):
                target = path.parent / relative
                if not target.resolve().is_relative_to(path.parent.resolve()):
                    raise OSError("Invalid archive sidecar path")
                if target.exists():
                    if json.loads(target.read_text(encoding="utf-8")) != data:
                        raise OSError("Conflicting compaction generation")
                else:
                    _atomic_write_json(str(target), data)
            if bundle.get("inputs") is not None:
                from .input_receipts import InputReceiptStore
                try:
                    InputReceiptStore(state_dir, sid).restore(bundle["inputs"])
                except Exception as exc:
                    raise OSError('Input originals cannot be restored') from exc
            raw = bundle["transcript"]
        else:
            db = archive_db_path(state_dir)
            if not db.is_file():
                return False
            con = sqlite3.connect(str(db))
            try:
                if not con.execute("SELECT 1 FROM chats WHERE chat_id=?", ("marionette:" + sid,)).fetchone():
                    return False
                raw = [{"role": role, "content": text} for role, text in con.execute(
                    "SELECT role, text FROM messages WHERE chat_id=? ORDER BY seq", ("marionette:" + sid,))]
            finally:
                con.close()
        _atomic_write_json(str(path), raw)
        return True
    except (OSError, ValueError, sqlite3.Error):
        return False


def archive_status(state_dir: str) -> Dict[str, Any]:
    db = archive_db_path(state_dir)
    chats = 0
    if db.is_file():
        con = sqlite3.connect(str(db))
        try:
            row = con.execute("SELECT COUNT(*) FROM chats").fetchone()
            chats = int(row[0]) if row else 0
        except sqlite3.Error:
            chats = 0
        finally:
            con.close()
    return {
        "ok": True,
        "archive_db": str(db),
        "chats": chats,
        "backup_dir": str(backup_dir(state_dir)),
        "vault_present": db.is_file(),
    }


def _chats_with_all_tokens(con: sqlite3.Connection, tokens: Sequence[str]) -> set:
    matched = None
    for token in tokens:
        found = set()
        try:
            for row in con.execute(
                """
                SELECT DISTINCT m.chat_id
                FROM messages_fts
                JOIN messages m ON m.id = messages_fts.rowid
                WHERE messages_fts MATCH ?
                """,
                (_fts_quote(token),),
            ):
                found.add(row[0])
        except sqlite3.OperationalError:
            for row in con.execute(
                "SELECT DISTINCT chat_id FROM messages WHERE instr(lower(text), ?) > 0",
                (token,),
            ):
                found.add(row[0])
            for row in con.execute(
                "SELECT chat_id FROM chats WHERE instr(lower(coalesce(name, '')), ?) > 0",
                (token,),
            ):
                found.add(row[0])
            if matched is None:
                matched = found
            else:
                matched &= found
            if not matched:
                return set()
            continue
        for row in con.execute(
            "SELECT chat_id FROM chats WHERE instr(lower(coalesce(name, '')), ?) > 0",
            (token,),
        ):
            found.add(row[0])
        matched = found if matched is None else matched & found
        if not matched:
            return set()
    return matched or set()


def search_archive(
    state_dir: str,
    query: str,
    *,
    limit: int = MAX_SEARCH,
    source: str = "",
) -> List[Dict[str, Any]]:
    tokens = distinctive_tokens(query)
    if not tokens:
        return []
    db = archive_db_path(state_dir)
    if not db.is_file():
        return []
    cap = max(1, min(int(limit or MAX_SEARCH), 50))
    src_filter = (source or "").strip()
    con = sqlite3.connect(str(db))
    try:
        chat_ids = _chats_with_all_tokens(con, tokens)
        if src_filter:
            allowed = {
                row[0]
                for row in con.execute("SELECT chat_id FROM chats WHERE source = ?", (src_filter,))
            }
            chat_ids &= allowed
        if not chat_ids:
            return []
        placeholders = ",".join("?" for _ in chat_ids)
        names = {
            row[0]: row[1]
            for row in con.execute(
                "SELECT chat_id, name FROM chats WHERE chat_id IN (%s)" % placeholders,
                tuple(chat_ids),
            )
        }
        hits: List[Dict[str, Any]] = []
        per_chat: Dict[str, int] = {}
        try:
            fts_sql = (
                "SELECT m.chat_id, c.name, m.text, c.source "
                "FROM messages_fts "
                "JOIN messages m ON m.id = messages_fts.rowid "
                "JOIN chats c ON c.chat_id = m.chat_id "
                "WHERE messages_fts MATCH ? AND m.chat_id IN (%s) "
                "ORDER BY m.id" % placeholders
            )
            rows = con.execute(fts_sql, (_fts_or(tokens),) + tuple(chat_ids)).fetchall()
        except sqlite3.OperationalError:
            rows = []
            for cid in chat_ids:
                for row in con.execute(
                    "SELECT chat_id, text FROM messages WHERE chat_id = ? LIMIT 20",
                    (cid,),
                ):
                    rows.append((row[0], names.get(cid, ""), row[1], cid.split(":", 1)[0]))
        needles = tuple(t.lower() for t in tokens)
        keyed = []
        for index, row in enumerate(rows):
            chat_id, name, text, src = row[0], row[1] or "", row[2] or "", row[3]
            count = per_chat.get(chat_id, 0)
            if count >= PER_CHAT_HITS:
                continue
            per_chat[chat_id] = count + 1
            name_boost = 0 if any(n in (name or "").lower() for n in needles) else 1
            snippet = " ".join(str(text).split())
            if len(snippet) > 240:
                snippet = snippet[:237] + "..."
            keyed.append((name_boost, index, {
                "chat_id": chat_id,
                "name": name or chat_id,
                "source": src,
                "snippet": snippet,
            }))
            if len(keyed) >= cap:
                break
        keyed.sort(key=lambda row: (row[0], row[1]))
        hits = [row[2] for row in keyed[:cap]]
        return hits
    finally:
        con.close()


def read_archived_chat(
    state_dir: str,
    chat_id: str,
    *,
    max_messages: int = MAX_READ_MESSAGES,
) -> Optional[Dict[str, Any]]:
    cid = (chat_id or "").strip()
    if not cid:
        return None
    db = archive_db_path(state_dir)
    if not db.is_file():
        return None
    cap = max(1, min(int(max_messages or MAX_READ_MESSAGES), 2000))
    con = sqlite3.connect(str(db))
    try:
        row = con.execute(
            "SELECT chat_id, source, origin_id, workspace, name, backup_path "
            "FROM chats WHERE chat_id = ?",
            (cid,),
        ).fetchone()
        if not row:
            return None
        msgs = con.execute(
            "SELECT role, text FROM messages WHERE chat_id = ? ORDER BY seq LIMIT ?",
            (cid, cap),
        ).fetchall()
        return {
            "chat_id": row[0],
            "source": row[1],
            "origin_id": row[2],
            "workspace": row[3] or "",
            "name": row[4] or row[0],
            "backup_path": row[5] or "",
            "messages": [{"role": m[0] or "", "text": m[1] or ""} for m in msgs],
        }
    finally:
        con.close()


def format_search_hits(hits: Sequence[Dict[str, Any]]) -> str:
    if not hits:
        return "No archived chats matched."
    lines = []
    for hit in hits:
        lines.append(
            "- %s  [%s]  %s\n  %s"
            % (hit.get("chat_id"), hit.get("source"), hit.get("name"), hit.get("snippet") or "")
        )
    lines.append("Read a hit with read_archived_chat chat_id=<id>.")
    return "\n".join(lines)


def format_archived_chat(payload: Optional[Dict[str, Any]]) -> str:
    if not payload:
        return "Archived chat not found."
    lines = [
        "# %s" % payload.get("name"),
        "chat_id: %s" % payload.get("chat_id"),
        "source: %s" % payload.get("source"),
        "",
    ]
    for msg in payload.get("messages") or []:
        role = (msg.get("role") or "message").strip() or "message"
        lines.extend(["## %s" % role, msg.get("text") or "", ""])
    backup = payload.get("backup_path") or ""
    if backup:
        lines.append("backup: %s" % backup)
    return "\n".join(lines)


def maybe_boot_ingest(state_dir: str, sessions: Optional[Iterable[Dict[str, Any]]] = None) -> None:
    """Best-effort ingest of archived sessions on harness start. Skipped under pytest."""
    if "PYTEST_CURRENT_TEST" in os.environ:
        return
    if not (state_dir or "").strip():
        return
    try:
        ingest_all(state_dir, sessions=sessions)
    except Exception:
        pass
