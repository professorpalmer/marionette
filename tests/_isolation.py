"""Scope a fake to one module instead of the whole test process.

Patching ``subprocess.Popen`` or ``threading.Thread`` through any path edits
the shared stdlib module, so every other thread in the process sees the fake
too: a harness server left running by an earlier test started its scheduler
inline under a patched Thread and deadlocked exit, and a background spawn
consumed a test's scripted fake processes ("pop from empty list"). Give the
module under test its own copy of the stdlib module instead.
"""
from __future__ import annotations

import contextlib
import importlib
import types
from unittest import mock


def isolate_module_attr(monkeypatch, module, attr: str, **overrides) -> types.SimpleNamespace:
    """Replace ``module.<attr>`` (a stdlib module it imported) with a private
    copy carrying ``overrides``, for this test only."""
    if isinstance(module, str):
        module = importlib.import_module(module)
    local = types.SimpleNamespace(**vars(getattr(module, attr)))
    for name, value in overrides.items():
        setattr(local, name, value)
    monkeypatch.setattr(module, attr, local)
    return local


@contextlib.contextmanager
def isolated_patch(module, attr: str, name: str, new=None, **mock_kwargs):
    """Context-manager form (like ``mock.patch``): yields the fake installed as
    ``module.<attr>.<name>`` for ``module`` only."""
    if isinstance(module, str):
        module = importlib.import_module(module)
    fake = new if new is not None else mock.MagicMock(**mock_kwargs)
    original = getattr(module, attr)
    local = types.SimpleNamespace(**vars(original))
    setattr(local, name, fake)
    setattr(module, attr, local)
    try:
        yield fake
    finally:
        setattr(module, attr, original)
