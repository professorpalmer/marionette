from __future__ import annotations

from .pilot_replacement import input_projection

"""Session-owned prompt playlist. A successful mutation has reached disk.

Human originals live in the native input document; this file is only the
runnable playlist projection. Receipt attempts precede consumption.
"""

import copy
from contextlib import contextmanager
import hashlib
import json
import os
import tempfile
import threading
import uuid
from typing import Optional

from . import seal
from .session_files import read_json, write_json


ORIGINAL_TEXT_UNSET = object()


class PromptQueueError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code

    def payload(self) -> dict:
        return {"ok": False, "code": self.code, "error": str(self)}


_OWNER_LOCKS: dict[tuple[str, str], threading.RLock] = {}
_OWNER_LOCKS_LOCK = threading.Lock()


class PromptQueueMixin:
    def bind_prompt_queue(self, state_root: str, session_id: str) -> None:
        """Bind once, after the real ID is known. Never consult the active view."""
        if not session_id:
            raise ValueError("prompt queue requires a session ID")
        owner = (os.path.normcase(os.path.realpath(state_root)), session_id)
        with self._prompt_queue_lock:
            previous = getattr(self, '_prompt_queue_owner', None)
            if previous is not None:
                if previous != owner:
                    raise PromptQueueError('queue_owner_changed', 'Prompt queue belongs to another session.')
                return
            with _OWNER_LOCKS_LOCK:
                lock = _OWNER_LOCKS.setdefault(owner, threading.RLock())
            self._prompt_queue_owner = owner
            self._prompt_queue_owner_lock = lock
            name = hashlib.sha256(session_id.encode('utf-8')).hexdigest() + '.json'
            self._prompt_queue_path = os.path.join(owner[0], 'prompt_queues', name)
            # No legacy import: unscoped input has no provable session owner.
            self._prompt_queue_legacy_paths = list(dict.fromkeys([
                os.path.join(owner[0], 'prompt_queue.json'),
                os.path.join(os.path.realpath(self.state_dir), 'prompt_queue.json'),
            ]))

    def _queue_lock(self):
        with self._prompt_queue_lock:
            if getattr(self, '_prompt_queue_owner', None) is None:
                # CLI without a GUI ID gets a unique durable invocation namespace.
                sid = getattr(self, 'harness_session_id', '') or 'cli-' + uuid.uuid4().hex
                self.bind_prompt_queue(self.state_dir, sid)
            return self._prompt_queue_owner_lock

    @contextmanager
    def _queue_transaction(self):
        # The process lock covers multiple runner objects. The file lock covers
        # separate backends; never unlink it (that would split lock ownership).
        with self._queue_lock():
            lock_file = None
            try:
                os.makedirs(os.path.dirname(self._prompt_queue_path), exist_ok=True)
                lock_file = open(self._prompt_queue_path + '.lock', 'a+b')
                if os.name == 'nt':
                    import msvcrt
                    if os.fstat(lock_file.fileno()).st_size == 0:
                        lock_file.write(b'0')
                        lock_file.flush()
                    lock_file.seek(0)
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
                else:
                    import fcntl
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            except OSError as exc:
                if lock_file is not None:
                    lock_file.close()
                raise PromptQueueError('queue_write_failed', 'Prompt queue could not be locked for saving. Your previous queue is unchanged.') from exc
            try:
                yield
            finally:
                # Closing releases the OS lock even after a failed mutation.
                lock_file.close()

    def _read_queue(self) -> list:
        owner = self._prompt_queue_owner
        try:
            data = read_json(self._prompt_queue_path, owner[1], 'prompt-queue')
        except FileNotFoundError:
            return []
        except (OSError, ValueError) as exc:
            raise PromptQueueError('queue_read_failed', 'Prompt queue cannot be read. Original file retained for recovery.') from exc
        if (not isinstance(data, dict) or data.get('version') != 1
                or data.get('session_id') != owner[1] or not isinstance(data.get('queue'), list)):
            raise PromptQueueError('queue_read_failed', 'Invalid prompt queue. Original file retained for recovery.')
        for item in data['queue']:
            if (not isinstance(item, dict) or not isinstance(item.get('id'), str)
                    or not item['id'] or not isinstance(item.get('text'), str)
                    or not isinstance(item.get('images'), list)
                    or any(not isinstance(p, str) for p in item['images'])
                    or not isinstance(item.get('model'), str)):
                raise PromptQueueError('queue_read_failed', 'Invalid prompt queue item. Original file retained for recovery.')
        ids = [item['id'] for item in data['queue']]
        if len(set(ids)) != len(ids):
            raise PromptQueueError('queue_read_failed', 'Duplicate prompt IDs. Original file retained for recovery.')
        return data['queue']

    def _commit_queue(self, items: list) -> None:
        """Caller holds owner lock across read, snapshot, write, and publication."""
        tmp = None
        payload = {'version': 1, 'session_id': self._prompt_queue_owner[1], 'queue': items}
        try:
            if seal.is_sealed(payload['session_id']):
                write_json(self._prompt_queue_path, payload, payload['session_id'], 'prompt-queue')
                self._prompt_queue = copy.deepcopy(items)
                return
            parent = os.path.dirname(self._prompt_queue_path)
            os.makedirs(parent, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', newline='\n',
                    dir=parent, prefix='.queue-', suffix='.tmp', delete=False) as f:
                tmp = f.name
                json.dump(payload, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self._prompt_queue_path)
        except (OSError, ValueError, TypeError) as exc:
            raise PromptQueueError('queue_write_failed', 'Prompt queue was not saved. Your previous queue is unchanged; keep your draft and retry.') from exc
        finally:
            if tmp is not None and os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
        self._prompt_queue = copy.deepcopy(items)

    def _load_prompt_queue(self) -> None:
        with self._queue_lock():
            self._prompt_queue = self._read_queue()

    def prompt_queue_recovery(self) -> list:
        """Read-only evidence, never a runnable projection or an implicit import."""
        with self._queue_lock():
            paths = [(p, 'legacy') for p in self._prompt_queue_legacy_paths]
            try:
                self._read_queue()
            except PromptQueueError:
                paths.append((self._prompt_queue_path, 'unreadable'))
            recovery = []
            for path, kind in paths:
                try:
                    with open(path, encoding='utf-8', errors='replace') as f:
                        content = f.read()
                except FileNotFoundError:
                    continue
                except OSError:
                    content = None
                if kind == 'legacy' and content is not None:
                    try:
                        if json.loads(content) == {'queue': []}:
                            continue
                    except ValueError:
                        pass
                recovery.append({'kind': kind, 'path': path, 'content': content})
            return recovery

    @contextmanager
    def _input_queue_transaction(self):
        from .input_receipts import session_input_store
        store = session_input_store(self)
        with store.transaction():
            with self._queue_transaction():
                yield store, store._read(), self._read_queue()

    @input_projection
    def enqueue_prompt(self, text: str, images: Optional[list] = None,
                       model: Optional[str] = None, *, source: str = '',
                       retry_key=None, documents=None, upload_root=None,
                       input_id=None, original_text=ORIGINAL_TEXT_UNSET) -> dict:
        delivery_text = text or ''
        text = delivery_text.strip()
        if not text and not images and not documents and not input_id:
            return {'id': '', 'text': '', 'images': [], 'model': ''}
        from .input_receipts import session_input_store
        store = session_input_store(self)
        with store.transaction():
            item = {'id': uuid.uuid4().hex, 'text': text or '(see attached file)',
                    'images': [str(p) for p in (images or []) if p and str(p).strip()],
                    'model': (model or '').strip(), 'owner_instance': store.instance}
            if source == 'goal_mode':
                item['source'] = source
            else:
                if input_id:
                    receipt = store.get(input_id)
                else:
                    receipt = store.admit(delivery_text, original_text=original_text, images=images, documents=documents,
                                          upload_root=upload_root or getattr(self, '_input_upload_root', None),
                                          model=item['model'], retry_key=retry_key)
                if 'delivery_text' in receipt:
                    item['text'] = receipt['delivery_text'].strip() or '(see attached file)'
                item['id'] = receipt['id']
                item['input_id'] = receipt['id']
                item['images'] = [a['ref'] for a in receipt['attachments'] if a['kind'] == 'image']
                item['documents'] = [a['ref'] for a in receipt['attachments'] if a['kind'] == 'document']
            with self._input_queue_transaction() as (store, document, items):
                if item.get('input_id'):
                    receipt = next(r for r in document['inputs'] if r['id'] == item['input_id'])
                    if receipt['status'] != 'accepted' or receipt['owner_instance'] != store.instance:
                        return {**item, 'held': True}
                existing = next((row for row in items if row['id'] == item['id']), None)
                if existing:
                    if item.get('input_id') and receipt.get('reason') == 'interrupt_followup' and existing.get('owner_instance') != store.instance:
                        existing['owner_instance'] = store.instance
                        self._commit_queue(items)
                    return copy.deepcopy(existing)
                self._commit_queue(items + [item])
            return copy.deepcopy(item)

    def list_prompts(self) -> list:
        with self._input_queue_transaction() as (store, document, items):
            receipts = {r['id']: r for r in document['inputs']}
            runnable = [item for item in items if self._prompt_is_runnable(item, store, receipts)]
            self._prompt_queue = copy.deepcopy(runnable)
            return copy.deepcopy(runnable)

    @staticmethod
    def _prompt_is_runnable(item, store, receipts):
        if item.get('owner_instance') != store.instance:
            return False
        if not item.get('input_id'):
            return item.get('source') == 'goal_mode'
        receipt = receipts.get(item['input_id'])
        return bool(receipt and receipt['status'] == 'accepted' and receipt['owner_instance'] == store.instance)

    def held_prompts(self) -> list:
        with self._input_queue_transaction() as (store, document, items):
            receipts = {r['id']: r for r in document['inputs']}
            return copy.deepcopy([item for item in items if not self._prompt_is_runnable(item, store, receipts)])

    def input_receipts(self) -> list:
        from .input_receipts import session_input_store
        return session_input_store(self).list()

    def remove_prompt(self, id: str) -> bool:
        with self._input_queue_transaction() as (store, document, items):
            remaining = [x for x in items if x['id'] != id]
            if len(remaining) == len(items):
                return False
            for row in document['inputs']:
                if row['id'] == id and row['status'] not in ('injected', 'dropped'):
                    row.update(status='dropped', reason='removed_from_playlist')
            store._write(document)
            self._commit_queue(remaining)
            return True

    def reorder_prompts(self, ordered_ids: list) -> list:
        with self._input_queue_transaction() as (store, document, items):
            by_id = {x['id']: x for x in items}
            ordered = []
            for id_ in ordered_ids or []:
                if id_ in by_id:
                    ordered.append(by_id.pop(id_))
            ordered.extend(by_id.values())
            self._commit_queue(ordered)
            receipts = {r['id']: r for r in document['inputs']}
            return copy.deepcopy([item for item in ordered if self._prompt_is_runnable(item, store, receipts)])

    def clear_prompts(self) -> int:
        with self._input_queue_transaction() as (store, document, items):
            ids = {item['id'] for item in items}
            for row in document['inputs']:
                if row['id'] in ids and row['status'] not in ('injected', 'dropped'):
                    row.update(status='dropped', reason='playlist_cleared')
            store._write(document)
            self._commit_queue([])
            return len(items)

    def _next_queued_needs_driver_swap(self) -> bool:
        items = self.list_prompts()
        return bool(items and items[0]['model'] and items[0]['model'] != str(self.config.driver or '').strip())

    def _pop_next_prompt(self) -> dict:
        return self.handoff_prompt()

    def handoff_prompt(self, input_id=None) -> dict:
        """Commit delivery attempt before consumption; never reclaim an old attempt."""
        with self._input_queue_transaction() as (store, document, items):
            receipts = {r['id']: r for r in document['inputs']}
            candidates = [item for item in items if self._prompt_is_runnable(item, store, receipts)]
            if not candidates:
                return {}
            item = candidates[0]
            if input_id is not None and item['id'] != input_id:
                raise PromptQueueError('input_handoff_conflict', 'Only the current live playlist head can be handed off.')
            if item.get('input_id'):
                token = uuid.uuid4().hex
                receipts[item['input_id']].update(status='delivering', reason='playlist_handoff',
                                                handoff_token=token, handoff_claimed=False, transcript_digest=store._transcript_digest())
                item['handoff_token'] = token
                store._write(document)
            self._commit_queue([row for row in items if row['id'] != item['id']])
            return copy.deepcopy(item)
