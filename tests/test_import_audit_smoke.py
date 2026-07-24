"""CI smoke: packaging-critical modules import without zlib/PYZ faults."""

from __future__ import annotations

import importlib


# Keep this list short and high-signal — full pkgutil walks of puppetmaster are
# slow and not what catches frozen-build header faults in harness/pmharness.
_CRITICAL = (
    "harness",
    "harness.conversation",
    "harness.server",
    "harness.worker",
    "harness.import_selftest",
    "pmharness",
    "pmharness.bridge",
    "pmharness.drivers.token_usage",
)


def test_critical_modules_import_cleanly():
    for name in _CRITICAL:
        importlib.import_module(name)
