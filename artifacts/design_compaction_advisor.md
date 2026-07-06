# Design: layer-pressure compaction advisor (Marionette Wave 8)

> Status: ADVISORY + OPT-IN TRIGGER (v1). Pure arithmetic from L0-L3 snapshots;
> no LLM in the advice path, no automatic spill deletion, no Puppetmaster changes.

## Context

Wave 7 (`harness/memory_layers.py`) records read-only L0-L3 snapshots per turn
and surfaces the latest snapshot on `/api/usage` and in `CostBreakdown`. Wave 8
adds a **compaction advisor** that turns layer pressure into actionable guidance
and (when explicitly enabled) can tighten the history-compaction trigger.

Today `_maybe_compact_history` fires at 75% of `max_context_tokens` using only
hot-context size. It ignores L1 spill pressure, L2 workspace growth, and L3
compaction savings already banked. The advisor fuses L0 hot ratio with L1
session state to recommend when compaction is advisable.

## Inputs

| Input | Source |
|-------|--------|
| L0-L3 snapshot dict | `latest_layer_snapshot(state_dir, session_id)` from `memory_layers.jsonl` |
| Token budget | `max_context_tokens` from session config (fallback 96000) |

The snapshot shape matches Wave 7: `L0`/`L1`/`L2`/`L3` each with `bytes`,
`entries`, optional `components`, plus `snapshot_at`.

## Pressure model

Implemented in `harness/compaction_advisor.py` as pure arithmetic (never raises).

**Hot ratio** (chars-per-token proxy, same as the rest of the harness):

```
hot_ratio = L0.bytes / (max_context_tokens * 4)
```

Clamped to `[0, 2]`.

**Advice levels** (evaluated in order; first match wins):

| Level | Condition | Constant |
|-------|-----------|----------|
| `now` | `hot_ratio >= 0.70` | `_HOT_NOW_RATIO` |
| `soon` | `hot_ratio >= 0.55` | `_HOT_SOON_RATIO` |
| `soon` | `hot_ratio >= 0.40` AND L1 bytes > 5 MB | `_L1_PRESSURE_BYTES` |
| `none` | otherwise | — |

Each fired rule appends one short plain-words string to `reasons`, e.g.
`"hot context at 72 percent of budget"` or a L1-pressure line when the
combined warm-context + heavy-session rule fires.

**Return shape** from `assess_layer_pressure`:

```json
{
  "level": "none|soon|now",
  "hot_ratio": 0.72,
  "l1_bytes": 5242880,
  "l3_reclaimed_bytes": 12000,
  "reasons": ["hot context at 72 percent of budget"]
}
```

`l3_reclaimed_bytes` = `max(0, compaction_chars_before - compaction_chars_after)`
from the L3 snapshot components (informational; does not affect level).

Malformed or empty snapshots return the `none` advice without raising.

## Surfacing

| Surface | Behavior |
|---------|----------|
| `advice_payload(state_dir, session_id, max_context_tokens)` | Loads latest snapshot, runs assessment, returns `{"compaction_advice": {...}}` or `{}` when disabled / no snapshot |
| `GET /api/usage` | Merges `advice_payload(...)` into session fields (best-effort try/except) |
| `CostBreakdown.tsx` | When `compaction_advice.level` is `soon` or `now`, one row: label "Compaction advice", value = level + first reason. Hidden for `none` or absent field |

**Kill switch (measurement/surfacing):** `HARNESS_COMPACTION_ADVISOR` defaults
**ON**. Set `0`, `false`, or `off` to disable; `advice_payload` then returns
`{}` and the usage API omits the key.

## Opt-in trigger adjustment

When `HARNESS_ADVISOR_COMPACTION` is enabled (default **OFF** — behavior change),
`_maybe_compact_history` reads the latest snapshot and calls
`assess_layer_pressure`. If the level is `now`, the compaction trigger moves
from `int(budget * 0.75)` to `int(budget * 0.65)` (`_ADVISED_TRIGGER_RATIO`).

Rules:

- Only when `force=False` (forced compaction path unchanged).
- Only ever **earlier** compaction; never delays the default trigger.
- Any failure in the advisor path leaves the default 0.75 trigger intact.
- Wrapped in try/except; never blocks or raises into `send()`.

## Integration map

| Piece | Location |
|-------|----------|
| Advisor module | `harness/compaction_advisor.py` (new) |
| Usage API merge | `harness/server.py` `_tool_output_savings_fields` |
| Trigger hook | `harness/conversation.py` `_maybe_compact_history` |
| UI row | `webapp/src/components/CostBreakdown.tsx` |
| Types | `webapp/src/lib/api.ts` |
| Tests | `tests/test_compaction_advisor.py` (new); extend usage + compaction tests |

## Test strategy

- Unit: threshold boundaries (one point each side of 0.40/0.55/0.70), L1-pressure
  promotion, malformed input, clamp, env kill switch, journal round-trip.
- API: `compaction_advice` present when journal + toggle on; absent when disabled.
- Trigger: env off unchanged; env on + `now` snapshot fires between 65–75%;
  patched raise leaves default trigger.

## Non-goals (this wave)

- Automatic spill deletion or retention enforcement.
- LLM calls in the advice path (pure arithmetic only).
- Changing the compaction summarizer prompt or split logic.
- Background threads, timers, or watchers.
- Puppetmaster repo changes.
- YAML anywhere.
