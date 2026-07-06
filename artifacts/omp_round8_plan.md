# Round 8 implementation plan (compaction advisor consuming memory layers)

Instructions for the implementing model. All work happens in
`C:\Users\pwall\Projects\marionette`. Work the tasks IN ORDER. One commit
per task with the exact message given. Run the suite
(`python -m pytest tests -q`) before every commit, and `npm run build` in
`webapp/` for any task that touches the frontend. Commit locally only:
do NOT push, do NOT tag, do NOT run `gh` — the user pushes after review.

Ground rules (same as rounds 4-7):

- No emojis anywhere. Python 3.9 floor: no `match`, no `X | Y` unions in
  runtime annotations; `from __future__ import annotations` at module top.
- JSON, never YAML. No new dependencies (stdlib only).
- Every new seam is best-effort: it must never raise into, block, or slow
  the `send()` hot path. Mirror the guard style in
  `harness/memory_layers.py` (per-layer try/except returning empties).
- Env-toggle kill switch for any behavior change; measurement-only code
  may default on, behavior-affecting code defaults OFF.
- Windows/macOS parity: `os.path.join`, no shell-specific commands, UTF-8
  with `errors="replace"` on every file read.
- The working tree has pre-existing untracked plan files under
  `artifacts/` (omp/rqgm rounds). Leave them alone; stage only files you
  create or edit for this plan.

## Context: why this wave

Round 7 (v0.7.47) added read-only L0-L3 memory layer snapshots
(`harness/memory_layers.py`): per-turn journal `memory_layers.jsonl`,
`snapshot_memory_layers()`, `latest_layer_snapshot()`, surfaced on
`/api/usage` and in the CostBreakdown UI. Measurement without a consumer
is shelfware. Round 8 builds the consumer: a **compaction advisor** that
turns layer pressure into an actionable recommendation, surfaces it, and
(opt-in only) lets it tighten the history-compaction trigger.

Today compaction triggers purely on hot-context size:
`Conversation._maybe_compact_history` (harness/conversation.py ~line
1153) fires at 75 percent of `max_context_tokens`. It knows nothing about
L1 spill pressure, L2 store growth, or how much compaction has already
saved (L3). The advisor fuses those signals.

Read first: `harness/memory_layers.py` (all of it — the snapshot shape is
the advisor's input), `harness/conversation.py` `_maybe_compact_history`
and the `send()` call site that invokes `record_memory_layer_snapshot`,
`harness/server.py` where `/api/usage` assembles its payload,
`harness/history_compaction_journal.py` (`summarize_history_compactions`),
`webapp/src/components/CostBreakdown.tsx` (the Round 7 memory-layer row),
and `tests/test_memory_layers.py` for the established test style.

## Task A: design note

1. Add `artifacts/design_compaction_advisor.md` covering: inputs (the
   L0-L3 snapshot dict + token budget), the pressure model (Task B),
   advice levels (`none` / `soon` / `now`), surfacing (usage API + UI),
   the opt-in trigger adjustment and its kill switch, and non-goals:
   no automatic spill deletion, no LLM in the advice path (pure
   arithmetic only), no change to the compaction summarizer itself, no
   background threads.
2. Commit: "Add design note for layer-pressure compaction advisor"

## Task B: pure advisor module

Goal: a deterministic, dependency-free function from snapshot to advice.

1. Add `harness/compaction_advisor.py`:
   - `advisor_enabled() -> bool`: env `HARNESS_COMPACTION_ADVISOR`,
     default ON for measurement/surfacing (values `0/false/off` disable).
   - `assess_layer_pressure(snapshot: dict, max_context_tokens: int)
     -> dict`. Pure function, never raises (malformed input returns the
     `none` advice). Returns:
     `{"level": "none"|"soon"|"now", "hot_ratio": float,
       "l1_bytes": int, "l3_reclaimed_bytes": int, "reasons": [...]}`.
     Model (keep the constants module-level and named):
     - `hot_ratio` = L0 bytes / (max_context_tokens * 4) — the same
       chars-per-token proxy the codebase already uses. Clamp to [0, 2].
     - level `now` when `hot_ratio >= 0.70` (`_HOT_NOW_RATIO`).
     - level `soon` when `hot_ratio >= 0.55` (`_HOT_SOON_RATIO`) OR
       (`hot_ratio >= 0.40` AND L1 bytes > 5 MB (`_L1_PRESSURE_BYTES`)) —
       heavy session state plus a warm context earns an early warning.
     - otherwise `none`.
     - `reasons` holds one short plain-words string per rule that fired,
       e.g. `"hot context at 72 percent of budget"`.
   - `advice_payload(state_dir: str, session_id: str,
     max_context_tokens: int) -> dict`: load `latest_layer_snapshot`,
     run `assess_layer_pressure`, return `{"compaction_advice": {...}}`;
     empty dict when disabled or no snapshot exists. Never raises.
2. Tests (`tests/test_compaction_advisor.py`): level boundaries at the
   three thresholds (test one point per side of each); L1-pressure path
   promotes `none` to `soon`; malformed/empty snapshot returns `none`
   without raising; clamp behavior; env kill switch makes
   `advice_payload` return `{}`; payload round-trips from a journal file
   written with `record_memory_layer_snapshot`.
3. Commit: "Add pure layer-pressure compaction advisor"

## Task C: surface advice on the usage API and UI

1. `harness/server.py`: where `/api/usage` assembles the Round 7
   memory-layer fields, also merge `advice_payload(...)` using the
   session's `max_context_tokens` (fall back to 96000 like
   `_maybe_compact_history` does). Best-effort try/except around the
   merge.
2. `webapp/src/lib/api.ts`: add the optional `compaction_advice` field to
   the usage type.
3. `webapp/src/components/CostBreakdown.tsx`: under the Round 7 memory
   layer row, when `compaction_advice.level` is `soon` or `now`, render
   one row: label "Compaction advice", value = the level plus the first
   reason string. No row when level is `none` or the field is absent.
   Match the existing row styling exactly; no new components.
4. Tests: extend the server usage-endpoint test to assert the
   `compaction_advice` key appears when a journal snapshot exists and the
   env toggle is on, and is absent when disabled. Run `npm run build`.
5. Commit: "Surface compaction advice on usage API and cost breakdown"

## Task D: opt-in early-compaction trigger

Goal: advice can tighten the compaction trigger — only when explicitly
enabled, and only ever EARLIER, never later.

1. In `harness/conversation.py` `_maybe_compact_history`: read env
   `HARNESS_ADVISOR_COMPACTION` (default OFF — this changes behavior).
   When enabled and not `force`: call `assess_layer_pressure` with the
   latest snapshot (guarded try/except; any failure means no adjustment).
   If advice level is `now`, use a trigger of `int(budget * 0.65)`
   (constant `_ADVISED_TRIGGER_RATIO = 0.65`) instead of 0.75. The
   default trigger and all other logic stay byte-for-byte identical when
   the env is off or advice is unavailable.
2. Tests: with env off, trigger math unchanged (existing tests keep
   passing untouched); with env on and a `now`-level snapshot journaled,
   compaction fires at a context size between 65 and 75 percent of
   budget where it previously would not; advisor errors (patch
   `assess_layer_pressure` to raise) leave the default trigger intact.
3. Commit: "Add opt-in advisor-driven early compaction trigger"

## Task E: version bump (local commit, NO push)

1. Bump to `0.7.48` in `pyproject.toml`, `harness/__init__.py`,
   `webapp/package.json`, `webapp/package-lock.json` (version fields).
2. `python -m pytest tests -q` and `npm run build` — both clean.
3. Commit: "chore(release): bump version to 0.7.48". Do NOT push, do NOT
   tag, do NOT run `gh`.

## Verification (after ALL tasks)

```powershell
cd C:\Users\pwall\Projects\marionette
python -m pytest tests -q
cd webapp; npm run build; cd ..
git log --oneline -6
# five new local commits, only pre-existing artifacts/*.md untracked.
# NO PUSH. NO TAG.
```

## Out of scope (do not do)

- Puppetmaster repo changes.
- Automatic spill deletion or retention enforcement.
- Changing the compaction summarizer prompt or split logic.
- Background threads, timers, or watchers.
- Pushing anything to origin.
