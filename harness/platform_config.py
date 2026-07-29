from __future__ import annotations

"""Single owner of the Marionette platform-lock (``platform.json``) location.

The platform lock decides which adapters (agentic / cursor / claude-code / ...)
may run. Puppetmaster resolves it with ``platform_lock.platform_config_path()``
— beside the model registry, so it follows ``PUPPETMASTER_MODELS_PATH``. The
harness used to hardcode ``~/.puppetmaster/platform.json`` instead, so once
``marionette_registry`` isolated the registry into ``~/.pmharness`` the two
halves of the product read DIFFERENT files: Settings > Platform wrote one
document while routing and ``PlatformLockedError`` consulted another, and every
adapter toggle silently failed to take effect.

Every Marionette read/write of the lock goes through this module so there is
exactly one answer to "where does platform.json live?".
"""

import json
import os
import tempfile
from typing import Any, Optional

from .diag import note as _diag

# Tests pin an exact file; honored before any registry-derived resolution.
TEST_PATH_ENV = "TEST_PLATFORM_JSON_PATH"

_LEGACY_PLATFORM_JSON = os.path.join(
    os.path.expanduser("~/.puppetmaster"), "platform.json"
)


def platform_json_path() -> str:
    """The canonical platform.json path for this process.

    Resolution order: ``TEST_PLATFORM_JSON_PATH`` (hermetic tests), then
    Puppetmaster's own ``platform_config_path()`` so the lock always sits beside
    the registry named by ``PUPPETMASTER_MODELS_PATH``, then the historical
    ``~/.puppetmaster/platform.json`` if Puppetmaster is not importable.
    """
    override = (os.environ.get(TEST_PATH_ENV) or "").strip()
    if override:
        return override
    try:
        from puppetmaster.platform_lock import platform_config_path

        return str(platform_config_path())
    except Exception as exc:
        _diag("platform_config.resolve", exc)
        return _LEGACY_PLATFORM_JSON


def legacy_platform_json_path() -> str:
    """The pre-isolation ``~/.puppetmaster/platform.json``."""
    return _LEGACY_PLATFORM_JSON


def read_platform_config(path: Optional[str] = None) -> dict:
    """Parse platform.json, or an empty dict when absent/corrupt."""
    target = path or platform_json_path()
    if not os.path.exists(target):
        return {}
    try:
        with open(target, "r", encoding="utf-8", errors="replace") as f:
            data = json.load(f)
    except Exception as exc:
        _diag("platform_config.read", exc, msg=target)
        return {}
    return data if isinstance(data, dict) else {}


def write_platform_config_atomic(path: str, data: dict) -> None:
    """Replace platform.json in one step so a crash cannot truncate the lock."""
    dir_path = os.path.dirname(path)
    if dir_path:
        os.makedirs(dir_path, exist_ok=True)
    tmp_fd, tmp_path = tempfile.mkstemp(dir=dir_path or ".", prefix="platform_")
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8", newline="\n") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, path)
    except Exception:
        if os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        raise


def _under_product_home(path: str) -> bool:
    """True when ``path`` lives in ``~/.pmharness`` or ``~/.puppetmaster``.

    Guards the legacy import so a temp / test lock path can never pull in the
    developer's real ``~/.puppetmaster/platform.json`` (same rule keys.py uses
    for its own legacy fallback).
    """
    try:
        target = os.path.normcase(os.path.abspath(path))
    except Exception:
        return False
    for home in ("~/.pmharness", "~/.puppetmaster"):
        root = os.path.normcase(os.path.abspath(os.path.expanduser(home)))
        if target.startswith(root + os.sep) or target == root:
            return True
    return False


def migrate_legacy_platform_config(
    canonical: Optional[str] = None,
    legacy: Optional[str] = None,
) -> dict[str, Any]:
    """Seed the canonical lock from ``~/.puppetmaster/platform.json`` once.

    Only when the canonical file is ABSENT. An existing canonical document is
    the operator's current choice — importing the shared Cursor/CLI lock over
    it would re-disable adapters they just enabled — so this never overwrites.

    ``canonical`` / ``legacy`` are for hermetic tests; when omitted, the default
    legacy import is restricted to canonical paths under a real product home.
    """
    report: dict[str, Any] = {"migrated": False, "reason": "", "path": ""}
    explicit = canonical is not None or legacy is not None
    canonical = canonical or platform_json_path()
    report["path"] = canonical
    legacy = legacy or legacy_platform_json_path()
    if not explicit and not _under_product_home(canonical):
        report["reason"] = "canonical outside product home"
        return report
    try:
        same_file = os.path.abspath(canonical) == os.path.abspath(legacy)
    except Exception:
        same_file = canonical == legacy
    if same_file:
        report["reason"] = "canonical is legacy path"
        return report
    if os.path.exists(canonical):
        report["reason"] = "canonical already configured"
        return report
    legacy_data = read_platform_config(legacy)
    if not legacy_data:
        report["reason"] = "no legacy config"
        return report
    try:
        write_platform_config_atomic(canonical, legacy_data)
    except Exception as exc:
        _diag("platform_config.migrate", exc)
        report["reason"] = str(exc)
        return report
    report["migrated"] = True
    return report
