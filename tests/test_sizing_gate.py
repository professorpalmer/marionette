"""Sizing reminder: once per turn, only for [parallel] plans, fails open."""
from __future__ import annotations

import time
import unittest
from types import SimpleNamespace
from unittest import mock

from harness import sizing_gate
from harness.todo import TodoItem, TodoPhase


def session(done: int, total: int, *, started_ago: float = 240.0, parallel: bool = True):
    prefix = "[parallel] " if parallel else ""
    tasks = [TodoItem(f"{prefix}region {i}", "completed" if i < done else "pending") for i in range(total)]
    return SimpleNamespace(
        _todo_phases=[TodoPhase("Build", tasks)],
        _busy_since=time.monotonic() - started_ago,
        _last_prompt_tokens=20_000,
        config=SimpleNamespace(no_delegation=False, max_context_tokens=400_000),
        pilot=SimpleNamespace(model="gpt-6.1-sol"),
    )


class SizingGateTests(unittest.TestCase):
    def test_handoff_advice_is_given_once_per_turn(self):
        s = session(2, 16)
        with mock.patch("pmharness.bridge.sizing_decision",
                        return_value={"action": "handoff", "advice": "SIZING: hand off"}) as decide:
            first = sizing_gate.sizing_provider(s)
            second = sizing_gate.sizing_provider(s)
        self.assertEqual(first.text, "SIZING: hand off")
        self.assertIsNone(second)
        plan = decide.call_args.args[0]
        self.assertEqual(sum(u["status"] == "done" for u in plan), 2)
        self.assertAlmostEqual(decide.call_args.kwargs["context_frac"], 0.05)

    def test_no_parallel_units_or_no_delegation_never_asks(self):
        with mock.patch("pmharness.bridge.sizing_decision") as decide:
            self.assertIsNone(sizing_gate.sizing_provider(session(2, 16, parallel=False)))
            quiet = session(2, 16)
            quiet.config.no_delegation = True
            self.assertIsNone(sizing_gate.sizing_provider(quiet))
        decide.assert_not_called()

    def test_stay_solo_or_missing_puppetmaster_is_silent(self):
        with mock.patch("pmharness.bridge.sizing_decision", return_value=None):
            self.assertIsNone(sizing_gate.sizing_provider(session(2, 16)))
        with mock.patch("pmharness.bridge.sizing_decision",
                        return_value={"action": "stay_solo", "advice": ""}):
            self.assertIsNone(sizing_gate.sizing_provider(session(4, 8)))

    def test_provider_is_registered(self):
        from harness.system_reminder import default_registry
        self.assertIn(sizing_gate.sizing_provider, default_registry().providers)


try:
    from puppetmaster import sizing as _pm_sizing  # noqa: F401
    HAVE_SIZING = True
except Exception:
    HAVE_SIZING = False


@unittest.skipUnless(HAVE_SIZING, "pinned Puppetmaster predates the sizing gate")
class RealGateTests(unittest.TestCase):
    def test_sixteen_regions_two_done_at_four_minutes_hands_off(self):
        reminder = sizing_gate.sizing_provider(session(2, 16))
        self.assertIsNotNone(reminder)
        self.assertIn("14 remaining independent units", reminder.text)

    def test_small_plan_stays_solo(self):
        self.assertIsNone(sizing_gate.sizing_provider(session(1, 3)))


if __name__ == "__main__":
    unittest.main()
