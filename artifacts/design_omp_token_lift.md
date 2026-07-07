# Design: OMP token-efficiency lift (Marionette Round 10)

> Status: IMPLEMENTED (v0.8.0). Three OMP policies ported into existing Marionette
> seams; no new storage, no embeddings, no image rendering, no ledger schema changes.

## Context

Marionette already has token-savings machinery from earlier OMP rounds:

- **Savings LEDGER** (`harness/tool_output_savings.py`) -- append-only SQLite records
  of chars compacted per `(session_id, tool_call_id)`.
- **Spill registry** (`harness/spill_registry.py`) -- indexes oversized tool outputs
  as `spill://` internal URIs.
- **History-compaction journal** -- records tokens reclaimed by `_maybe_compact_history`.
- **Compaction advisor** (`harness/compaction_advisor.py`) -- pure-arithmetic L0-L3
  pressure assessment surfaced on `/api/usage`.

Round 10 closes three gaps identified by auditing oh-my-pi (MIT, read-only reference
at `C:\Users\pwall\Projects\oh-my-pi`):

1. Absolute-token advisor thresholds (ratio-only advice fires too late on 200k+ windows).
2. Shared savings gate for tool-output offload (no floor/margin; tiny results get touched).
3. Per-turn output budget directive (`+Nk` advisory, `+Nk!` hard).

## Policy 1: absolute-token advisor thresholds

### OMP source

`packages/coding-agent/src/modes/components/status-line/context-thresholds.ts`

OMP computes effective percent thresholds as
`min(percent_threshold, token_threshold / context_window)`. On a 1M-token window,
a 55% ratio rule never fires until 550k tokens are in context, while the absolute
rule fires at 150k.

### Mechanism

In `assess_layer_pressure`, effective hot thresholds become:

```
effective_now  = min(_HOT_NOW_RATIO,  _HOT_NOW_TOKENS  / budget)
effective_soon = min(_HOT_SOON_RATIO, _HOT_SOON_TOKENS / budget)
```

Constants: `_HOT_NOW_TOKENS = 270_000`, `_HOT_SOON_TOKENS = 150_000`.

When the absolute threshold is the binding constraint, the reason string names it
explicitly, e.g. `"hot context above 150000 tokens on a large window"`.

Env overrides `HARNESS_ADVISOR_NOW_TOKENS` / `HARNESS_ADVISOR_SOON_TOKENS`: invalid
values fall back to the constant; zero or negative disables the absolute rule so
pure ratios apply.

The L1-combo rule (`hot_ratio >= 0.40` AND L1 > 5 MB) is unchanged but evaluated
against the effective soon threshold.

### Marionette seam

`harness/compaction_advisor.py` -- `assess_layer_pressure` only. Surfacing and
opt-in early-compaction trigger (`HARNESS_ADVISOR_COMPACTION`) are unchanged.

### Non-goals

- No change to compaction summarizer prompt or split logic.
- No LLM in the advice path.
- No automatic spill deletion.

## Policy 2: shared savings gate for tool-output offload

### OMP source

`packages/coding-agent/src/session/snapcompact-inline.ts`

```typescript
const MIN_TOOL_RESULT_TOKENS = 3000;
const SAVINGS_MARGIN = 0.9;
```

OMP never rasterizes tool results under 3000 tokens and only swaps when image tokens
are at most 90% of text tokens. Marionette mirrors the floor/margin semantics for
text spill/compaction (no image rendering).

### Mechanism

New module `harness/offload_policy.py`:

| Symbol | Default | Meaning |
|--------|---------|---------|
| `MIN_TOOL_RESULT_TOKENS` | 3000 | Floor: results below this token estimate are never offloaded |
| `SAVINGS_MARGIN` | 0.9 | Replacement must cost at most 90% of original chars |

Env overrides: `HARNESS_OFFLOAD_MIN_TOKENS`, `HARNESS_OFFLOAD_MARGIN` (defensive parse).

Pure functions (never raise):

- `should_offload(original_chars, replacement_chars) -> bool`
- `gate_decision(...) -> {"offload", "reason", "estimated_tokens_saved"}`

`estimate_tokens` is reused from `tool_output_savings` so the gate and ledger
agree on token math.

### Marionette seam

Wired into `harness/context_budget.py`:

- `maybe_persist_result` -- before returning a spill/truncation replacement, consult
  `should_offload`; when False, return the original content verbatim and skip the
  compaction callback (no ledger row).
- `enforce_turn_budget` -- inherits the gate via `maybe_persist_result`.

Call path from `harness/conversation.py`:

- `_append_action_result` -> `maybe_persist_result` + `_tool_output_compaction_callback`
- post-action batch -> `enforce_turn_budget`

### Non-goals

- Snapcompact image rendering, vision, or PNG anything.
- Embeddings or new storage backends.
- Schema changes to the savings ledger (still records the same fields when offload applies).

## Policy 3: per-turn output token budget directive

### OMP source

`packages/coding-agent/src/modes/turn-budget.ts`

Regex anchored to token boundaries:
`(?:^|\s)\+(\d+(?:\.\d+)?)([km])?(!)?(?=\s|$)`

- `+50k` -- advisory output budget for the turn.
- `+50k!` -- hard ceiling: stop the tool-call loop when cumulative output tokens exceed it.

### Mechanism

New module `harness/turn_budget.py`:

- `parse_turn_budget(text) -> {"total": int, "hard": bool} | None`
- Non-finite or non-positive values return None.
- `$+5k` and `v+2` do not match (no whitespace-bounded `+` token).

In `harness/conversation.py`, when a user message arrives:

1. Parse budget (when `HARNESS_TURN_BUDGET` is on, default ON).
2. Stash on the session for the turn (`_turn_budget`, `_turn_output_tokens`).
3. **Advisory**: append one system-side note to the outgoing request:
   `"output budget for this turn: N tokens"` (no truncation).
4. **Hard**: after each assistant response, if cumulative `_turn_output_tokens`
   exceeds the budget, finish the turn without raising and surface
   `turn_budget_exhausted` in the usage payload / `assistant_done` event.

### Marionette seam

`harness/conversation.py` -- `_send_locked_inner` (parse on entry, note in sys prompt,
check after each pilot response). `get_context_usage` merges turn-budget fields.

### Non-goals

- Frontend surfacing of advice/budget in the webapp (later round).
- Truncating model output to enforce advisory budgets (advisory is prompt-only).

## Integration map

| Piece | Location |
|-------|----------|
| Advisor absolute thresholds | `harness/compaction_advisor.py` |
| Offload gate | `harness/offload_policy.py` (new) |
| Gate wiring | `harness/context_budget.py` |
| Turn budget parser | `harness/turn_budget.py` (new) |
| Turn budget loop hook | `harness/conversation.py` |
| Feature flags doc | `artifacts/feature_flags.md` |
| Tests | `tests/test_compaction_advisor.py`, `tests/test_offload_policy.py` (new), `tests/test_turn_budget.py` (new) |

## Test strategy

- Advisor: 32k window unchanged; 1M window + 160k-token L0 fires `soon` via absolute
  rule; env zero restores ratio-only; invalid env ignored.
- Offload gate: below-floor no-op; above-margin rejected; big/small stub accepted with
  ledger dedupe; env overrides; garbage inputs never raise.
- Turn budget: parser unit tests (`+50k`, `+1.5m!`, embedded `+5k`, non-matches);
  one conversation test that hard budget stops the loop early with mocked driver.
