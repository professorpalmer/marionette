"""Growth-fence for the residual ``_send_locked_inner`` body in send_loop.py.

This is optional post-tranche debt tracking, not a claim that the send-loop
peel is finished. CI fails only when the method grows beyond the ratcheted
ceiling, so existing size is allowed while uncontrolled growth is blocked.
"""

from __future__ import annotations

from pathlib import Path

_SEND_LOOP = Path(__file__).resolve().parents[1] / "harness" / "send_loop.py"
_METHOD_MARKER = "def _send_locked_inner"
# Ratchet: max(700, ceil(623 * 1.05)) at introduction — 623 lines from def to EOF.
_MAX_SEND_LOCKED_INNER_LINES = 700


def _send_locked_inner_line_count() -> int:
    text = _SEND_LOOP.read_text(encoding="utf-8")
    lines = text.splitlines()
    start = next(
        i for i, line in enumerate(lines) if line.lstrip().startswith(_METHOD_MARKER)
    )
    # _send_locked_inner is the last method in SendLoopMixin; count to EOF.
    return len(lines) - start


def test_send_locked_inner_within_growth_fence():
    current = _send_locked_inner_line_count()
    assert current <= _MAX_SEND_LOCKED_INNER_LINES, (
        f"_send_locked_inner grew to {current} lines "
        f"(max {_MAX_SEND_LOCKED_INNER_LINES}). "
        "Peel new logic out instead of expanding the residual body."
    )
