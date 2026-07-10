"""pm-harness product core: the PM-native harness.

This is the productization of the validated research (pmharness/). The Session
drives Puppetmaster's Orchestrator in-process via a swappable open-weights
driver, with a budget and REAL artifact feedback (not eval fixtures). The
DurableState read layer is what the GUI renders.

Driver default: glm-5.2 (MIT, efficient, 100% on the discriminating eval).
"""
__version__ = "0.9.29"

# On Windows, default every subprocess to a hidden console. The backend runs
# console-less under Electron; without this, each git/node/uv/puppetmaster
# child pops a visible terminal window. See harness/win_console.py.
from .win_console import hide_child_consoles as _hide_child_consoles

_hide_child_consoles()
