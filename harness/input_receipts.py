from __future__ import annotations

"""Native, non-runnable human input originals and delivery evidence."""

import base64
import copy
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import uuid

from .prompt_queue import ORIGINAL_TEXT_UNSET, PromptQueueError


class InputReceiptError(PromptQueueError):
    pass


_LOCKS = {}
_LOCKS_GUARD = threading.Lock()
_INPUT_LOCAL = threading.local()
# Admission limits match the upload default and eight-image composer cap.
_MAX_FILE_BYTES = 10 * 1024 * 1024
_MAX_INPUT_BYTES = 80 * 1024 * 1024
_MAX_ATTACHMENTS = 32
_MAX_IMAGES = 8
_CHUNK_BYTES = 64 * 1024

_STATUSES = {'accepted', 'delivering', 'injected', 'dropped', 'uncertain'}


class InputReceiptStore:
    def __init__(self, state_root: str, session_id: str):
        from .compaction_archive import safe_session_id
        if not session_id or safe_session_id(session_id) != session_id:
            raise InputReceiptError('input_owner_invalid', 'A canonical session ID is required.')
        self.root = os.path.realpath(state_root)
        self.session_id = session_id
        self.path = Path(self.root) / 'transcripts' / (session_id + '.inputs.json')
        self.blob_dir = self.path.parent / (session_id + '.input-originals')
        self.instance = uuid.uuid4().hex
        with _LOCKS_GUARD:
            self.lock = _LOCKS.setdefault(str(self.path), threading.RLock())

    @contextmanager
    def transaction(self):
        from .native_publication import native_publication
        # Publication -> owner thread -> owner OS lock. Never callbacks/yields
        # to application code while held; the yield here is internal only.
        try:
            with native_publication(self.root), self.lock:
                held = getattr(_INPUT_LOCAL, 'held', None)
                if held is None:
                    held = _INPUT_LOCAL.held = {}
                key = str(self.path)
                if key in held:
                    yield
                    return
                try:
                    self.path.parent.mkdir(parents=True, exist_ok=True)
                    descriptor = os.open(str(self.path) + '.lock', os.O_RDWR | os.O_CREAT, 0o600)
                    handle = os.fdopen(descriptor, 'r+b')
                    if os.name == 'nt':
                        import msvcrt
                        if os.fstat(handle.fileno()).st_size == 0:
                            handle.write(b'0')
                            handle.flush()
                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                except OSError as exc:
                    if 'handle' in locals():
                        handle.close()
                    raise InputReceiptError('input_lock_failed', 'Input storage is unavailable; keep your draft.') from exc
                held[key] = handle
                try:
                    yield
                finally:
                    del held[key]
                    handle.close()
        except OSError as exc:
            raise InputReceiptError('input_storage_unavailable', 'Native input storage is unavailable; keep your draft.') from exc

    def validate(self, data):
        def require(condition):
            if not condition:
                raise ValueError('invalid input document')
        try:
            require(isinstance(data, dict) and data['version'] in (1, 2))
            require(data['session_id'] == self.session_id and isinstance(data['inputs'], list))
            require('blobs' not in data or isinstance(data['blobs'], dict))
            ids = set()
            keys = set()
            for row in data['inputs']:
                require(isinstance(row, dict))
                require(isinstance(row['id'], str) and row['id'] and row['id'] not in ids)
                ids.add(row['id'])
                require(row['status'] in _STATUSES)
                require(isinstance(row['original_text'], str) and isinstance(row['model'], str))
                require('delivery_text' not in row or isinstance(row['delivery_text'], str))
                require(isinstance(row['owner_instance'], str) and bool(row['owner_instance']))
                require(isinstance(row['reason'], str) and isinstance(row['attachments'], list))
                key = row.get('retry_key')
                if key is not None:
                    require(isinstance(key, str) and bool(key) and key not in keys)
                    keys.add(key)
                for attachment in row['attachments']:
                    require(isinstance(attachment['name'], str) and attachment['kind'] in ('image', 'document'))
                    require(isinstance(attachment['sha256'], str) and len(attachment['sha256']) == 64
                            and all(c in '0123456789abcdef' for c in attachment['sha256']))
                    require(type(attachment['byte_length']) is int and 0 <= attachment['byte_length'] <= _MAX_FILE_BYTES)
                    if data['version'] == 1:
                        require(isinstance(attachment['base64'], str)
                                and len(attachment['base64']) <= 4 * ((_MAX_FILE_BYTES + 2) // 3))
                        raw = base64.b64decode(attachment['base64'], validate=True)
                        require(len(raw) == attachment['byte_length'])
                        require(hashlib.sha256(raw).hexdigest() == attachment['sha256'])
                    else:
                        require('base64' not in attachment)
                    require(attachment['ref'] == 'input:' + self.session_id + ':' + attachment['sha256'])
                if data['version'] == 1:
                    require(row['payload_digest'] == self._digest(row['original_text'], row['attachments'], row['model'], row.get('delivery_text')))
                else:
                    require(row['metadata_digest'] == self._metadata_digest(row))
        except (KeyError, TypeError, ValueError) as exc:
            raise InputReceiptError('input_corrupt', 'Input originals cannot be verified. Evidence was retained unchanged.') from exc
        return data

    def _read(self):
        try:
            data = json.loads(self.path.read_text(encoding='utf-8'))
        except FileNotFoundError:
            # A missing manifest can be rebuilt from the verified
            # native bundle. Never turn corrupt archive evidence into emptiness.
            from .chat_archive import _verified_raw
            try:
                bundle = _verified_raw(self.root, 'marionette:' + self.session_id)
                if bundle is not None and bundle.get('inputs') is not None:
                    archived = self.validate(bundle['inputs'])
                    self._check_recovery_revision(self._manifest(archived))
                    data = self._import_originals(archived)
                    self._write(data)
                    return data
            except InputReceiptError:
                raise
            except Exception as exc:
                raise InputReceiptError('input_archive_unavailable', 'Archived input originals cannot be verified; no empty replacement was created.') from exc
            marker_handle = _INPUT_LOCAL.held[str(self.path)]
            marker_handle.seek(0)
            revision = marker_handle.read()
            # Empty / 0-byte orphan lock with no inputs.json: bootstrap a fresh
            # document so the next admit can proceed honestly.
            if revision in (b'', b'0'):
                return {'version': 2, 'session_id': self.session_id, 'inputs': []}
            # R-marker (or other non-empty digest) without a recoverable archive
            # or inputs.json: clear the lock so we do not loop on a silent vague
            # failure, then raise an honest recovery code.
            self._reset_orphan_lock(marker_handle)
            raise InputReceiptError(
                'input_document_missing',
                'Previously retained input originals are missing and no verified '
                'bundle can restore them. Review your draft, then open or pick a '
                'project session before sending again.',
            )
        except (OSError, ValueError) as exc:
            raise InputReceiptError('input_read_failed', 'Input originals cannot be read. Evidence was retained unchanged.') from exc
        self.validate(data)
        if data['version'] == 1:
            data = self._import_originals(data)
            self._write(data)
        return data

    def _write(self, data):
        from .compaction_archive import _atomic_write_json
        self.validate(data)
        try:
            _atomic_write_json(str(self.path), data)
            # The persistent lock inode also records that disappearance is not
            # a new empty session. It contains no original or runnable data.
            from .compaction_archive import json_digest
            marker = _INPUT_LOCAL.held[str(self.path)]
            marker.seek(0)
            marker.write(b'R' + json_digest(data).encode('ascii'))
            marker.truncate()
            marker.flush()
            os.fsync(marker.fileno())
        except (OSError, ValueError) as exc:
            raise InputReceiptError('input_commit_uncertain', 'Input publication did not complete verification; keep your draft and inspect receipts before retrying.') from exc


    @staticmethod
    def _reset_orphan_lock(marker_handle):
        """Clear a non-empty lock marker after an unrecoverable missing document.

        Leaves a bootstrap-ready empty/0 marker so a later admit can create a
        fresh inputs.json instead of failing closed forever on the stale digest.
        """
        marker_handle.seek(0)
        marker_handle.write(b'0')
        marker_handle.truncate()
        marker_handle.flush()
        try:
            os.fsync(marker_handle.fileno())
        except OSError:
            pass

    def _check_recovery_revision(self, document):
        from .compaction_archive import json_digest
        marker = _INPUT_LOCAL.held[str(self.path)]
        marker.seek(0)
        revision = marker.read()
        if revision not in (b'', b'0') and revision != b'R' + json_digest(document).encode('ascii'):
            raise InputReceiptError('input_archive_stale', 'The archive does not match the last committed input revision; original evidence was not replaced.')

    @staticmethod
    def _metadata_digest(row):
        from .compaction_archive import json_digest
        payload = {key: row[key] for key in
                   ('original_text', 'attachments', 'model', 'payload_digest')}
        if 'delivery_text' in row:
            payload['delivery_text'] = row['delivery_text']
        return json_digest(payload)

    @staticmethod
    def _manifest(document):
        return {key: value for key, value in document.items() if key != 'blobs'}

    @staticmethod
    def _bounded_read(handle):
        chunks = []
        size = 0
        while True:
            chunk = handle.read(min(_CHUNK_BYTES, _MAX_FILE_BYTES + 1 - size))
            if not chunk:
                return b''.join(chunks)
            size += len(chunk)
            if size > _MAX_FILE_BYTES:
                raise InputReceiptError('input_attachment_limit', 'Attachments are limited to 10 MiB each; keep your draft.')
            chunks.append(chunk)

    def _blob_bytes(self, item):
        try:
            path = self.blob_dir / item['sha256']
            if self.blob_dir.is_symlink() or path.is_symlink():
                raise ValueError('linked original')
            raw = self._read_upload(str(path), str(self.blob_dir))
            if len(raw) != item['byte_length'] or hashlib.sha256(raw).hexdigest() != item['sha256']:
                raise ValueError('original digest mismatch')
            return raw
        except (OSError, ValueError) as exc:
            raise InputReceiptError('input_attachment_corrupt', 'Retained original is missing or changed; receipt metadata remains available.') from exc

    def _publish_blob(self, item, raw):
        try:
            if len(raw) != item['byte_length'] or hashlib.sha256(raw).hexdigest() != item['sha256']:
                raise InputReceiptError('input_attachment_corrupt', 'Original bytes do not match their digest.')
            if self.blob_dir.is_symlink():
                raise InputReceiptError('input_attachment_corrupt', 'Original storage cannot be a symbolic link.')
            self.blob_dir.mkdir(parents=True, exist_ok=True)
            path = self.blob_dir / item['sha256']
            if path.exists() or path.is_symlink():
                self._blob_bytes(item)
                return
            fd, temporary = tempfile.mkstemp(dir=str(self.blob_dir))
            try:
                with os.fdopen(fd, 'wb') as handle:
                    handle.write(raw)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, path)
                if os.name != 'nt':
                    fd = os.open(str(self.blob_dir), os.O_RDONLY)
                    try:
                        os.fsync(fd)
                    finally:
                        os.close(fd)
                self._blob_bytes(item)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        except OSError as exc:
            raise InputReceiptError('input_commit_uncertain', 'Original publication did not complete; keep your draft and inspect receipts before retrying.') from exc

    def _import_originals(self, document):
        """Verify the complete transport before publishing any authoritative file."""
        self.validate(document)
        payloads = {}
        for row in document['inputs']:
            hydrated = []
            for item in row['attachments']:
                encoded = item['base64'] if document['version'] == 1 else document.get('blobs', {}).get(item['sha256'])
                if not isinstance(encoded, str) or len(encoded) > 4 * ((_MAX_FILE_BYTES + 2) // 3):
                    raise InputReceiptError('input_attachment_corrupt', 'Archive original is missing or exceeds the attachment limit.')
                try:
                    raw = base64.b64decode(encoded, validate=True)
                except ValueError as exc:
                    raise InputReceiptError('input_attachment_corrupt', 'Archive original encoding is invalid.') from exc
                if len(raw) != item['byte_length'] or hashlib.sha256(raw).hexdigest() != item['sha256']:
                    raise InputReceiptError('input_attachment_corrupt', 'Archive original digest does not match.')
                payloads[item['sha256']] = (item, encoded)
                hydrated.append({**item, 'base64': encoded})
            if row['payload_digest'] != self._digest(row['original_text'], hydrated, row['model'], row.get('delivery_text')):
                raise InputReceiptError('input_corrupt', 'Archive input identity does not match its originals.')
        for item, encoded in payloads.values():
            path = self.blob_dir / item['sha256']
            if path.exists() or path.is_symlink():
                self._blob_bytes(item)
        for item, encoded in payloads.values():
            self._publish_blob(item, base64.b64decode(encoded, validate=True))
        result = copy.deepcopy(self._manifest(document))
        result['version'] = 2
        for row in result['inputs']:
            for item in row['attachments']:
                item.pop('base64', None)
            row['metadata_digest'] = self._metadata_digest(row)
        return result

    @staticmethod
    def _digest(text, attachments, model, delivery_text=None):
        from .compaction_archive import json_digest
        payload = {'text': text, 'attachments': attachments, 'model': model}
        if delivery_text is not None:
            payload['delivery_text'] = delivery_text
        return json_digest(payload)

    @staticmethod
    def _read_upload(path, root):
        resolved = os.path.realpath(path)
        if os.path.commonpath([root, resolved]) != root:
            raise ValueError('outside upload storage')
        if os.name == 'nt':
            descriptor = os.open(resolved, os.O_RDONLY | getattr(os, 'O_BINARY', 0))
        else:
            # Anchor every component to an opened upload directory; a raced
            # parent symlink must not redirect a read outside owned uploads.
            directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                parts = os.path.relpath(resolved, root).split(os.sep)
                for part in parts[:-1]:
                    child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
                    os.close(directory)
                    directory = child
                descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            finally:
                os.close(directory)
        with os.fdopen(descriptor, 'rb') as handle:
            import stat
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise ValueError('not a regular file')
            if info.st_size > _MAX_FILE_BYTES:
                raise InputReceiptError('input_attachment_limit', 'Attachments are limited to 10 MiB each; keep your draft.')
            return InputReceiptStore._bounded_read(handle)

    @staticmethod
    def _check_budget(budget):
        if budget[1] > _MAX_INPUT_BYTES:
            raise InputReceiptError('input_attachment_limit', 'An input supports at most 80 MiB of attachments; keep your draft.')

    def _retain(self, sources, upload_root, kind, budget):
        result = []
        root = os.path.realpath(upload_root or os.path.join(tempfile.gettempdir(), 'harness-uploads'))
        for source in sources or []:
            budget[0] += 1
            if budget[0] > _MAX_ATTACHMENTS:
                raise InputReceiptError('input_attachment_limit', 'An input supports 32 attachments; keep your draft.')
            path = (source.get('ref') or source.get('path')) if isinstance(source, dict) else source
            name = source.get('name', '') if isinstance(source, dict) else ''
            if not isinstance(path, str) or not path or not isinstance(name, str):
                raise InputReceiptError('input_attachment_invalid', 'An uploaded attachment reference is required.')
            if path.startswith('input:'):
                with self.transaction():
                    item = next((a for row in self._read()['inputs'] for a in row['attachments']
                                 if a['ref'] == path and a['kind'] == kind), None)
                    if item is None:
                        raise InputReceiptError('input_attachment_unknown', 'Attachment is not owned by this session.')
                    item = copy.deepcopy(item)
                    budget[1] += item['byte_length']
                    self._check_budget(budget)
                    item['base64'] = base64.b64encode(self._blob_bytes(item)).decode('ascii')
                    if name:
                        item['name'] = name
                    result.append(item)
                continue
            try:
                raw = self._read_upload(path, root)
            except (OSError, ValueError, TypeError) as exc:
                raise InputReceiptError('input_attachment_unavailable', 'An original attachment is missing or outside upload storage; keep your draft.') from exc
            budget[1] += len(raw)
            self._check_budget(budget)
            digest = hashlib.sha256(raw).hexdigest()
            if isinstance(source, dict) and ((source.get('sha256') is not None and source['sha256'] != digest)
                    or (source.get('byte_length') is not None and source['byte_length'] != len(raw))):
                raise InputReceiptError('input_attachment_corrupt', 'Uploaded attachment does not match its digest or length; keep your draft.')
            result.append({'ref': 'input:' + self.session_id + ':' + digest,
                           'sha256': digest, 'byte_length': len(raw), 'upload_path': path,
                           'name': name or os.path.basename(path), 'kind': kind,
                           'base64': base64.b64encode(raw).decode('ascii')})
        return result

    def admit(self, text, *, original_text=ORIGINAL_TEXT_UNSET, images=None, documents=None, upload_root=None, model='', retry_key=None, input_id=None):
        if original_text is not ORIGINAL_TEXT_UNSET and not isinstance(original_text, str):
            raise InputReceiptError('input_invalid', 'Original text must be a string.')
        if not isinstance(text, str) or (retry_key is not None and (not isinstance(retry_key, str) or not retry_key)):
            raise InputReceiptError('input_invalid', 'Text and optional retry key must be strings.')
        if images is not None and not isinstance(images, (list, tuple)):
            raise InputReceiptError('input_invalid', 'Images must be a list.')
        if documents is not None and not isinstance(documents, (list, tuple)):
            raise InputReceiptError('input_invalid', 'Documents must be a list of uploaded references.')
        if len(images or []) > _MAX_IMAGES or len(images or []) + len(documents or []) > _MAX_ATTACHMENTS:
            raise InputReceiptError('input_attachment_limit', 'An input supports eight images and 32 total attachments; keep your draft.')
        with self.transaction():
            budget = [0, 0]
            retained_images = self._retain(images, upload_root, 'image', budget)
            retained_documents = self._retain(documents, upload_root, 'document', budget)
            from .mention_context import extract_mention_tokens
            root = os.path.realpath(upload_root or os.path.join(tempfile.gettempdir(), 'harness-uploads'))
            explicit = {a.get('upload_path') for a in retained_documents}
            for token in extract_mention_tokens(text):
                if os.path.isabs(token) and token not in explicit:
                    try:
                        owned = os.path.commonpath([root, os.path.abspath(token)]) == root
                        owned = owned or os.path.commonpath([root, os.path.realpath(token)]) == root
                    except ValueError:
                        owned = False
                    if owned:
                        retained_documents.extend(self._retain([token], upload_root, 'document', budget))
                        explicit.add(token)
            attachments = retained_images + retained_documents
            literal = text if original_text is ORIGINAL_TEXT_UNSET else original_text
            delivery = None if original_text is ORIGINAL_TEXT_UNSET else text
            digest = self._digest(literal, attachments, model, delivery)
            data = self._read()
            retained = {a['sha256']: a for r in data['inputs'] for a in r['attachments']}
            for item in attachments:
                if item['sha256'] in retained:
                    self._blob_bytes(retained[item['sha256']])
            for row in data['inputs']:
                if retry_key is not None and row.get('retry_key') == retry_key:
                    if row['payload_digest'] != digest:
                        raise InputReceiptError('input_retry_conflict', 'This retry key already belongs to a different payload.')
                    reusable = (
                        row['status'] == 'accepted'
                        and row.get('owner_instance') == self.instance
                    )
                    if reusable:
                        return copy.deepcopy(row)
                    row['retry_key'] = None
                    continue
                if input_id is not None and row['id'] == input_id:
                    if row['payload_digest'] != digest:
                        raise InputReceiptError('input_id_conflict', 'Input identity has different originals.')
                    return copy.deepcopy(row)
            row = {'id': input_id or uuid.uuid4().hex, 'original_text': literal,
                   'attachments': attachments, 'model': model, 'payload_digest': digest,
                   'retry_key': retry_key, 'status': 'accepted', 'reason': '',
                   'created_at': time.time(), 'owner_instance': self.instance}
            if delivery is not None:
                row['delivery_text'] = delivery
            for item in row['attachments']:
                self._publish_blob(item, base64.b64decode(item.pop('base64'), validate=True))
            row['metadata_digest'] = self._metadata_digest(row)
            data['inputs'].append(row)
            self._write(data)
            return copy.deepcopy(row)

    def get(self, input_id):
        with self.transaction():
            for row in self._read()['inputs']:
                if row['id'] == input_id:
                    return copy.deepcopy(row)
        raise InputReceiptError('input_unknown', 'Unknown input receipt.')

    def materialize(self, ref):
        """Disposable provider file derived exclusively from verified native bytes."""
        with self.transaction():
            attachments = [a for row in self._read()['inputs'] for a in row['attachments']]
            item = next((a for a in attachments if a['ref'] == ref), None)
            if item is None:
                raise InputReceiptError('input_attachment_unknown', 'Attachment is not owned by this session.')
            raw = self._blob_bytes(item)
        suffix = Path(item['name']).suffix.lower()
        if not suffix or not suffix[1:].isalnum() or len(suffix) > 16:
            suffix = '.bin'
        target = self.path.parent / (self.session_id + '.input-materialized') / (item['sha256'] + suffix)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(dir=str(target.parent))
            try:
                with os.fdopen(fd, 'wb') as handle:
                    handle.write(raw)
                os.replace(temporary, target)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
            return str(target)
        except OSError as exc:
            raise InputReceiptError('input_attachment_unavailable', 'Retained attachment could not be prepared for delivery; keep your draft.') from exc

    def _transcript_digest(self):
        path = self.path.with_name(self.session_id + '.json')
        try:
            raw = path.read_bytes()
            data = json.loads(raw)
            if not isinstance(data, (dict, list)):
                raise ValueError('invalid transcript')
            return hashlib.sha256(raw).hexdigest()
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            raise InputReceiptError('input_transcript_unreadable', 'Native transcript is unreadable; original evidence was retained.') from exc

    def _durable_ids(self):
        from .compaction_archive import compaction_archive_path, load_compaction_archive_page
        path = self.path.with_name(self.session_id + '.json')
        rows = []
        try:
            if path.exists():
                raw = json.loads(path.read_text(encoding='utf-8'))
                rows.extend(raw.get('history', []) if isinstance(raw, dict) else raw)
            if os.path.exists(compaction_archive_path(self.root, self.session_id)):
                offset = 0
                while True:
                    page, total = load_compaction_archive_page(self.root, self.session_id, offset=offset, limit=400)
                    rows.extend(page)
                    offset += len(page)
                    if offset >= total:
                        break
                    if not page:
                        raise ValueError('incomplete archive')
        except (OSError, ValueError, TypeError) as exc:
            raise InputReceiptError('input_evidence_unreadable', 'Native delivery evidence is unreadable; no status was inferred.') from exc
        ids = set()
        for row in rows:
            if row.get('role') == 'user':
                if row.get('input_id'):
                    ids.add(row['input_id'])
                ids.update(row.get('input_ids') or [])
        return ids

    def list(self):
        with self.transaction():
            document = self._read()
            pending = [r for r in document['inputs'] if r['status'] in ('delivering', 'uncertain')]
            if pending:
                ids = self._durable_ids()
                changed = False
                for row in pending:
                    # Same-instance delivering is still the live attempt.
                    # Only a foreign owner (backend restart / new store) may
                    # be reconciled from durable transcript evidence.
                    if row['id'] in ids and row.get('owner_instance') != self.instance:
                        row.update(status='injected', reason='exact_native_input_id_reconciled')
                        changed = True
                if changed:
                    self._write(document)
            rows = copy.deepcopy(document['inputs'])
        for row in rows:
            row['held'] = row['owner_instance'] != self.instance or row['status'] in ('uncertain', 'dropped')
            if row['owner_instance'] != self.instance and row['status'] in ('accepted', 'delivering'):
                row['reason'] = 'cold_input_held_for_review'
                if row['status'] == 'delivering':
                    row['status'] = 'uncertain'
            for key in ('owner_instance', 'handoff_token', 'handoff_claimed', 'transcript_digest', 'metadata_digest'):
                row.pop(key, None)
            row['attachments'] = [{k: v for k, v in a.items() if k != 'base64'} for a in row['attachments']]
        return rows

    def transition(self, input_id, status, reason=''):
        if status not in ('delivering', 'dropped', 'uncertain'):
            raise InputReceiptError('input_transition_invalid', 'Injection requires strict transcript publication.')
        with self.transaction():
            data = self._read()
            row = next((r for r in data['inputs'] if r['id'] == input_id), None)
            if row is None:
                raise InputReceiptError('input_unknown', 'Unknown input receipt.')
            if row['status'] == status and status == 'dropped':
                return
            if row['status'] in ('injected', 'dropped'):
                raise InputReceiptError('input_terminal', 'This input has already reached a terminal state.')
            if status == 'delivering' and (row['status'] != 'accepted' or row['owner_instance'] != self.instance):
                raise InputReceiptError('input_held', 'Old or attempted input is held for review; copy to a new draft.')
            row.update(status=status, reason=reason)
            if status == 'delivering':
                row['transcript_digest'] = self._transcript_digest()
            self._write(data)

    @staticmethod
    def _preserves_history(before, after, field=''):
        if isinstance(before, dict):
            return isinstance(after, dict) and all(key in after and InputReceiptStore._preserves_history(value, after[key], key)
                                                   for key, value in before.items())
        if isinstance(before, list):
            return isinstance(after, list) and len(after) >= len(before) and all(
                InputReceiptStore._preserves_history(old, new, field) for old, new in zip(before, after))
        if isinstance(before, str) and field in ('content', 'text', 'reasoning_content', 'arguments'):
            return isinstance(after, str) and after.startswith(before)
        return before == after

    def publish_injected(self, input_ids, transcript):
        from .history_compaction_journal import recover_compaction_commit
        from .sessions import _write_transcript
        rows = transcript.get('history', [])
        ids = {r.get('input_id') for r in rows if r.get('role') == 'user'}
        for row in rows:
            if row.get('role') == 'user':
                ids.update(row.get('input_ids') or [])
        if not set(input_ids).issubset(ids):
            raise InputReceiptError('input_evidence_missing', 'Exact input IDs are missing from native history.')
        with self.transaction():
            data = self._read()
            selected = [r for r in data['inputs'] if r['id'] in input_ids]
            if len(selected) != len(set(input_ids)):
                raise InputReceiptError('input_transition_invalid', 'Input has no current delivery attempt.')
            statuses = {r['status'] for r in selected}
            if statuses <= {'injected'}:
                return
            if any(r['status'] not in ('delivering', 'injected') for r in selected):
                raise InputReceiptError('input_transition_invalid', 'Input has no current delivery attempt.')
            try:
                recover_compaction_commit(self.root, self.session_id)
                current_digest = self._transcript_digest()
                if any(row.get('transcript_digest') != current_digest for row in selected):
                    if not set(input_ids).issubset(self._durable_ids()):
                        raise InputReceiptError('input_publication_conflict', 'Native history changed during delivery; original input is held for review.')
                else:
                    path = self.path.with_name(self.session_id + '.json')
                    if path.exists():
                        current = json.loads(path.read_text(encoding='utf-8'))
                        if isinstance(current, dict) and current.get('pruned'):
                            raise InputReceiptError('input_restore_required', 'Explicitly restore the archived session before submitting a turn.')
                        history = current.get('history', []) if isinstance(current, dict) else current
                        if not self._preserves_history(history, transcript.get('history', [])):
                            raise InputReceiptError('input_publication_conflict', 'The native transcript contains history absent from this runner; reload before submitting.')
                        if isinstance(current, dict):
                            transcript = {**current, **transcript}
                    _write_transcript(self.root, self.session_id, transcript)
            except Exception as exc:
                for row in selected:
                    row.update(status='uncertain', reason=getattr(exc, 'code', 'native_publication_failed'))
                try:
                    self._write(data)
                except InputReceiptError:
                    pass  # The durable attempt marker still retains the originals.
                if isinstance(exc, InputReceiptError):
                    raise
                raise InputReceiptError('input_delivery_uncertain', 'Transcript publication is uncertain; input is retained for review.') from exc
            for row in selected:
                row.update(status='injected', reason='native_transcript_published')
            self._write(data)

    def retire_after_stop(self, preserve_input_id=None):
        """Hold the abandoned admission generation; never resume its attempts."""
        next_instance = uuid.uuid4().hex
        with self.transaction():
            try:
                document = self._read()
                changed = False
                for row in document['inputs']:
                    if row['owner_instance'] != self.instance:
                        continue
                    if row['status'] == 'accepted':
                        if row['id'] == preserve_input_id:
                            # Only the explicit incoming interrupt/follow-up is
                            # preserved; attempted inputs are never reopened.
                            row.update(owner_instance=next_instance, reason='interrupt_followup')
                        else:
                            row.update(status='dropped', reason='stop')
                        changed = True
                    elif row['status'] == 'delivering':
                        row.update(status='uncertain', reason='stop_during_delivery')
                        changed = True
                if changed:
                    self._write(document)
            finally:
                # Fence the abandoned generation before releasing its owner lock.
                self.instance = next_instance

    def prepare_delivery(self, input_id, *, handoff_token=None):
        """One attempt per receipt. A claimed handoff cannot be claimed again."""
        with self.transaction():
            document = self._read()
            row = next((r for r in document['inputs'] if r['id'] == input_id), None)
            if row is None or row['owner_instance'] != self.instance:
                raise InputReceiptError('input_held', 'Input is held for review; copy it to a new draft.')
            if row['status'] == 'delivering' and handoff_token and row.get('handoff_token') == handoff_token and not row.get('handoff_claimed'):
                row['handoff_claimed'] = True
            elif row['status'] == 'accepted' and not handoff_token:
                row.update(status='delivering', reason='delivery_started')
            else:
                raise InputReceiptError('input_already_attempted', 'Input already has a delivery attempt; inspect the receipt.')
            row['transcript_digest'] = self._transcript_digest()
            self._write(document)
            return copy.deepcopy(row)

    def delivery_content(self, input_id, text=None):
        """Add retained documents to canonical or server-enriched delivery text.

        API admission resolves input IDs to the frozen base before enrichment;
        callers may then supply trusted mention, annotation or auto context.
        """
        from .mention_context import read_file_mention
        row = self.get(input_id)
        if text is None:
            text = row.get('delivery_text', row['original_text'])
        images = []
        documents = []
        total = 0
        for attachment in row['attachments']:
            path = self.materialize(attachment['ref'])
            if attachment['kind'] == 'image':
                images.append(path)
            else:
                block, added = read_file_mention(path, attachment['name'], total_size=total)
                total += added
                documents.append(block)
        return ('\n\n'.join(documents) + '\n\n' + text if documents else text), images

    def attachment(self, ref):
        with self.transaction():
            for row in self._read()['inputs']:
                for item in row['attachments']:
                    if item['ref'] == ref:
                        return self._blob_bytes(item)
        raise InputReceiptError('input_attachment_unknown', 'Attachment is not owned by this session.')

    def snapshot(self):
        with self.transaction():
            document = self._read()
            if not self.path.exists():
                return None
            document = copy.deepcopy(document)
            blobs = {}
            for row in document['inputs']:
                for item in row['attachments']:
                    if item['sha256'] not in blobs:
                        blobs[item['sha256']] = base64.b64encode(self._blob_bytes(item)).decode('ascii')
                hydrated = [{**item, 'base64': blobs[item['sha256']]} for item in row['attachments']]
                if row['payload_digest'] != self._digest(row['original_text'], hydrated, row['model'], row.get('delivery_text')):
                    raise InputReceiptError('input_corrupt', 'Input identity does not match retained originals.')
            document['blobs'] = blobs
            return document

    def restore(self, document):
        self.validate(document)
        with self.transaction():
            if not self.path.exists():
                self._check_recovery_revision(self._manifest(document))
            document = self._import_originals(document)
            if self.path.exists():
                current = self._read()
                by_id = {row['id']: row for row in current['inputs']}
                for row in document['inputs']:
                    if row['id'] in by_id:
                        if by_id[row['id']]['payload_digest'] != row['payload_digest']:
                            raise InputReceiptError('input_restore_conflict', 'Existing input originals differ; evidence retained.')
                    else:
                        current['inputs'].append(copy.deepcopy(row))
                document = current
            self._write(document)


def publish_session_injected(session, input_ids):
    """Persist the live runner transcript, then publish receipt IDs onto it.

    Steer and follow-up send used to call ``publish_injected`` on an in-memory
    export while native disk still held a pre-compact or pre-sanitize
    snapshot. ``_preserves_history`` correctly rejects that. Sync disk first
    so the check compares the same residual the runner holds.
    """
    from .sessions import persist_live_transcript

    state_dir = getattr(session, "state_dir", None)
    session_id = getattr(session, "harness_session_id", None)
    if state_dir and session_id:
        persist_live_transcript(session, state_dir, session_id)
    session_input_store(session).publish_injected(
        list(input_ids), session.export_transcript_data(),
    )


def session_input_store(session):
    store = getattr(session, '_input_receipts', None)
    if store is not None:
        return store
    with session._prompt_queue_lock:
        store = getattr(session, '_input_receipts', None)
        if store is None:
            session._queue_lock()
            root, sid = session._prompt_queue_owner
            store = InputReceiptStore(root, sid)
            session._input_receipts = store
        return store
