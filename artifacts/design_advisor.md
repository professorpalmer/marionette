# Design: advisor (opt-in second-opinion pass)

## Problem

The pilot sometimes queues an action list with an obvious footgun (deleting a
file it just read wrong, running a destructive command against the wrong
directory). A cheap read-only review of the *pending action list* can catch
these before execution — but it must never block, never rewrite actions, and
never add latency when disabled.

## Shape

- New module `harness/advisor.py` (stdlib only).
- `advise(actions, repo, driver) -> list[str]`:
  - Builds one fixed prompt listing the pending actions (kind + salient
    argument per line, capped at 20 actions / 2000 chars).
  - Calls `driver.complete(prompt, system=...)` once. The driver is the
    session pilot object (`complete(prompt, system=None)` protocol) — no new
    model plumbing.
  - Parses the response as a JSON array of strings. Anything unparseable,
    empty, or erroring yields `[]`. Warnings are truncated to 200 chars each,
    max 5.
- Wiring in `harness/conversation.py`: after the pilot turn's actions are
  parsed and before the action loop, when `HARNESS_ADVISOR=1` and the turn
  has actions, compute warnings (hard cap: one advisor call per turn).
  Warnings are attached to the first `action_result` event of the turn as
  `advisor_warnings` — advisory only; execution proceeds regardless.

## Integration map

| Piece | Location |
| --- | --- |
| Advisor module | `harness/advisor.py` (new) |
| Call site | `harness/conversation.py` `_send_locked` action-loop preamble |
| Event surfacing | `send()` event pass-through (same place duration_ms is stamped) |
| Tests | `tests/test_advisor.py` (new) |

## Kill switch

`HARNESS_ADVISOR=1` enables (default OFF). Total advisor failure yields zero
warnings and no exception.

## Non-goals (v1)

- Blocking or auto-rewriting actions.
- Per-action advice or multi-round advisor dialogue.
- A separate advisor model/driver (uses the session pilot).

## Acceptance

- Disabled by default: no driver call is made.
- Enabled with a fake driver returning a JSON array: warnings surface on the
  first action_result of the turn.
- Garbage/exception from the driver yields no warnings and no exception.
