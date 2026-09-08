"""Registry-owned metadata view. Polls read a capture, never live server globals."""
from __future__ import annotations

from dataclasses import dataclass
import json
import secrets
import threading

from .job_metadata_capability import (
    ActiveContext, ViewChanged, UNAVAILABLE_REASON,
    bounded_metadata_available, create_metadata_reader,
)


def discover_sources(state_dir, repo):
    from .job_readmodel import discover_sources as discover
    return discover(state_dir, repo)


@dataclass(frozen=True)
class ViewTarget:
    session_id: str = ''
    repo: str = ''
    state_dir: str = ''


class MetadataView:
    """Short publication lock, persistent reader, explicitly refreshed sources.

    Registry mutations acquire lock after the pilot/replacement gates. The
    reader callback acquires only this lock. Never call reader methods, factories,
    discovery, or external cleanup while holding it.
    """

    def __init__(self):
        self.supported = bounded_metadata_available()
        self.lock = threading.RLock()
        self._target = ViewTarget()
        self._context = ActiveContext('', '', secrets.token_hex(16))
        self._local_handle = None
        self._sources = None
        self._reason = 'view_unavailable' if self.supported else UNAVAILABLE_REASON
        self._discovery = None
        self._reader = create_metadata_reader(self.capture) if self.supported else None

    def capture(self):
        with self.lock:
            return self._context

    def reader(self):
        with self.lock:
            return self._reader

    def select(self, session_id='', repo='', state_dir='', *, force=False, local_handle=None):
        target = ViewTarget(session_id, repo, state_dir)
        with self.lock:
            if force or target != self._target or local_handle is not self._local_handle:
                self._target = target
                self._local_handle = local_handle
                self._rotate('sources_not_refreshed' if session_id and repo and state_dir else 'view_unavailable')

    def invalidate(self):
        # An in-progress configuration transition has no authoritative context.
        with self.lock:
            previous = self._target
            previous_handle = self._local_handle
            self.select(force=True)
            return self._context.generation, previous, previous_handle

    def restore(self, transition):
        """Restore a rejected transition only if no newer publisher intervened."""
        generation, previous, local_handle = transition
        with self.lock:
            if self._context.generation == generation:
                self.select(previous.session_id, previous.repo, previous.state_dir, force=True, local_handle=local_handle)

    def invalidate_sources(self):
        with self.lock:
            self._rotate("sources_not_refreshed")

    def replace_root(self, state_dir, *, local_handle=None):
        with self.lock:
            target = self._target
            self.select(target.session_id, target.repo, state_dir, force=True, local_handle=local_handle)

    def _rotate(self, reason, sources=None):
        target = self._target
        self._context = ActiveContext(target.session_id, target.repo, secrets.token_hex(16))
        self._sources = sources
        self._reason = reason if self.supported else UNAVAILABLE_REASON
        self._reader = (create_metadata_reader(self.capture, sources, self._local_handle)
                        if self.supported else None)

    def describe(self):
        with self.lock:
            ctx = self._context
            result = dict(version=1, context=dict(session_id=ctx.session_id, repo=ctx.repo,
                          view_generation=ctx.generation), availability='unavailable', sources=[],
                          local=(self._local_handle.describe() if self.supported and self._local_handle is not None
                                 else dict(available=False, incarnation=None)),
                          missing=[self._reason], refreshing=self._discovery == ctx.generation)
            if self._sources is not None:
                result.update(availability='known', missing=[], sources=[
                    dict(source=s.selection.source, state_id=s.selection.state_id,
                         cross_project=s.cross_project, available=s.handle is not None)
                    for s in self._sources.stores])
            if len(json.dumps(result).encode()) > 16384:
                return dict(version=1, availability='unavailable', sources=[], missing=['response_budget'])
            return result

    def refresh(self, generation):
        with self.lock:
            if generation != self._context.generation:
                raise ViewChanged()
            if not self.supported:
                return 200, self.describe()
            if self._discovery is not None:
                return 409, dict(code='refresh_in_progress')
            target = self._target
            if not (target.session_id and target.repo and target.state_dir):
                return 200, self.describe()
            self._rotate('source_discovery_unavailable')
            ticket = self._context.generation
            self._discovery = ticket
        try:
            try:
                sources = discover_sources(target.state_dir, target.repo)
            except Exception:
                sources = None
            with self.lock:
                if self._context.generation != ticket:
                    raise ViewChanged()
                if sources is not None and sources.stores:
                    # Publication changes the source set: it gets its own epoch
                    # and exactly one reader, just like the pending view.
                    self._rotate('', sources)
                elif sources is not None:
                    self._reason = 'no_known_sources'
                self._discovery = None
                return 200, self.describe()
        finally:
            with self.lock:
                if self._discovery == ticket:
                    self._discovery = None


def get_view(qs, view):
    if qs:
        return 400, dict(code='invalid_read_request')
    return 200, view.describe()


def refresh_view(body, view):
    if (not isinstance(body, dict) or body.keys() != {'view_generation'}
            or not isinstance(body['view_generation'], str)
            or not 1 <= len(body['view_generation']) <= 128):
        return 400, dict(code='invalid_read_request')
    try:
        return view.refresh(body['view_generation'])
    except ViewChanged:
        return 409, dict(code='view_changed')
