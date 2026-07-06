# Round 7 implementation plan (L0-L3 memory layering study in Marionette)

Instructions for the implementing model. All work happens in
`C:\Users\pwall\Projects\marionette`. Work the tasks IN ORDER. One commit per
task with the exact message given. Run the suite
(`python -m pytest tests -q`) before every commit. Push to main at the end and
wait for CI green on ALL legs (ubuntu/macOS/windows x Python 3.9/3.11, plus
frontend-build). Do NOT tag — the user cuts releases after review.

Ground rules (same as rounds 4-6):

- No emojis anywhere. Python 3.9 floor: no `match`, no `X | Y` unions in
  runtime annotations; `from __future__ import annotations` at module top.
- JSON, never YAML. No new dependencies.
- Best-effort hooks swallow their own failures; measurement/journal seams are
  best-effort. Do not block conversation send or worker completion on layer
  accounting failures.
- Subprocess capture sets `encoding="utf-8", errors="replace"`.
- Windows: manual tempdir cleanup where `TemporaryDirectory` recurses on 3.9
  (see `test_agent_tools.py` pattern).
- PowerShell: `;` separates commands, never `&&`.

Context: Puppetmaster RQGM Waves 5-8 shipped (v1.5.0 through v1.7.0): evaluator
slots, epoch freezing, review-gate consumption, draft feedback loop. Marionette
Wave 6 (0.7.46) landed turn-context journal, advisor, AST preview, and
persistent eval history. Wave 6's roadmap deferred a **TencentDB-style L0-L3
memory layering study** until spill offload measurements existed — they do:
`harness/spill_registry.py`, spill fields on `/api/usage`, and
`tool_output_savings` ledgers are live.

This wave is **study + instrumentation**, not a full memory engine rewrite. Map
what Marionette already has into L0-L3, add a read-only layer snapshot at each
turn, surface it in the API/UI, and document promotion criteria for future
layers — no automatic tier migration, no remote storage, no Puppetmaster changes.

Read first: `artifacts/design_memory_offload.md`,
`artifacts/spill_offload_acceptance.md`, `harness/spill_registry.py`,
`harness/turn_context.py`, `harness/eval_history.py`,
`harness/tool_output_savings.py`, `harness/conversation.py`
(`_spill_usage_fields`, `enforce_turn_budget`), and `harness/server.py`
(`/api/usage` payload assembly).

## Task A: design note

1. Create `artifacts/design_memory_layers.md` defining Marionette's L0-L3 map:
   - **L0 (hot context):** tokens in the active model prompt for the current
     turn — conversation history slice, pending actions, uncompacted tool
     results still inline. Ephemeral; not durable across process restart.
   - **L1 (session durable):** state-dir artifacts tied to one harness session
     — `turn_context.jsonl`, `spill_index.sqlite` + `pmharness-results/`,
     eval-history DB, savings ledgers, advisor warnings journal (if any).
   - **L2 (workspace durable):** repo-scoped memory the harness reuses across
     sessions in the same workspace — NL memory graph, Puppetmaster job store
     when bridged, `.codegraph/` context cache (read-only reference; do not
     duplicate indexing here).
   - **L3 (cold / compacted):** intentionally shrunk or archived context —
     history compaction journal output, expired spills (when retention runs),
     stitched summaries from completed swarms referenced by URI only.
   For each layer: what is stored, typical size drivers, existing modules,
   and **non-goals for v1** (no auto-promotion between layers).
2. Include a "measurement v1" section: one JSON snapshot per turn with byte/
   entry counts per layer (not token-exact LLM counts — approximate and cheap).
3. Commit: "Add design note for L0-L3 memory layering study"

## Task B: layer snapshot module

Goal: pure, testable functions that classify and count without I/O beyond
reading known state-dir files.

1. Add `harness/memory_layers.py`:
   - `LAYER_IDS = ("L0", "L1", "L2", "L3")` — stable string ids.
   - `estimate_l0_hot_chars(conversation) -> int`: sum char lengths of
     messages in the active history window the session would send on next
     `send()` (reuse whatever `conversation` already exposes for history —
     do not duplicate token counting logic from `context_budget.py`; chars
     are the v1 proxy).
   - `measure_l1_session(state_dir, session_id) -> dict`: counts from
     `turn_context.jsonl` lines for session, spill registry
     (`spill_usage_payload`), eval history (`eval_history_payload`), and
     savings ledger rows if cheap to count — return
     `{"bytes": int, "entries": int, "components": {...}}`.
   - `measure_l2_workspace(state_dir, repo) -> dict`: best-effort counts for
     NL memory store file(s) under state_dir and any workspace memory index
     the harness already maintains; return zeros when absent (never raise).
   - `measure_l3_cold(state_dir, session_id) -> dict`: bytes in compaction
     journal artifacts + expired-sweep-eligible spill rows (count only; do
     not run sweep).
   - `snapshot_memory_layers(conversation, state_dir, session_id, repo="")
     -> dict`: assemble `{"L0": {...}, "L1": {...}, "L2": {...}, "L3": {...},
     "snapshot_at": iso}` with consistent keys (`bytes`, `entries`,
     `components` where applicable).
   All measurement failures for a layer return `{"bytes": 0, "entries": 0,
   "components": {}}` for that layer only.
2. Tests in `tests/test_memory_layers.py`: L0 increases when history grows;
   L1 reflects registered spill; L2/L3 return empty dicts without error when
   stores missing; full snapshot shape.
3. Commit: "Add read-only L0-L3 memory layer snapshot helpers"

## Task C: turn hook + API surfacing

Goal: every `send()` records layer snapshot alongside turn context.

1. In `harness/conversation.py` `send()`, after `record_turn_context(...)`,
   call a new best-effort `record_memory_layer_snapshot(state_dir,
   session_id, turn, snapshot)` in `memory_layers.py` that appends one JSON
   line per turn to `{state_dir}/memory_layers.jsonl` (same discipline as
   `turn_context.py`: open/write/close, swallow errors).
2. Add `layer_snapshot_at(state_dir, session_id, turn) -> dict | None` for
   replay/inspection.
3. In `harness/server.py`, extend `/api/usage` (or the same payload helper
   `_tool_output_savings_fields` uses) with latest layer snapshot summary:
   `memory_layers` key carrying the most recent snapshot dict or `{}`.
4. Tests: sending records a journal line; API payload includes memory_layers;
   missing journal degrades to `{}`.
5. Commit: "Journal memory layer snapshots per turn and expose on usage API"

## Task D: UI row (minimal)

Goal: operators see layer breakdown without reading JSONL.

1. In `webapp/src/components/CostBreakdown.tsx`, add optional
   `memory_layers?: Record<string, { bytes?: number; entries?: number }>` to
   the data interface. When L1 bytes > 0, render one compact row:
   `Memory layers: L0 … | L1 … | L2 … | L3 …` using human-readable byte
   counts (reuse any existing byte-format helper in the component file; if
   none, simple `(n/1024).toFixed(1) KB` is fine).
2. Wire from the usage fetch hook if not already passing through full payload.
3. `npm run build` must pass.
4. Commit: "Show memory layer snapshot summary in cost breakdown UI"

## Task E: release prep (bump, push, NO tag)

1. Bump to `0.7.47` in `pyproject.toml`, `harness/__init__.py`,
   `webapp/package.json`, `webapp/package-lock.json`.
2. Full verification: `python -m pytest tests -q` green locally; `npm run
   build` in `webapp/`.
3. Commit: "chore(release): bump version to 0.7.47", push to main, wait for
   CI green (`gh run watch <id> --exit-status`). Report the run id.
4. Do NOT tag, do NOT publish installers — user-owned after review.

## Verification (after ALL tasks)

```powershell
cd C:\Users\pwall\Projects\marionette
python -m pytest tests -q
cd webapp; npm run build
git push origin main
# gh run watch <run-id> --exit-status
# NO TAG.
```

## Out of scope (do not do)

- Puppetmaster repo changes.
- Automatic promotion/demotion of content between layers.
- Remote blob storage or cross-machine layer sync.
- Token-exact L0 measurement (chars proxy only in v1).
- Replacing spill offload or turn-context journals.
- YAML anywhere. Pushing any tag.

## After Wave 7 (roadmap)

- **Wave 8 (Marionette):** layer-aware compaction policy — use L1/L3 metrics
  to trigger history compaction hints (advisory only, env-gated).
- **Wave 9 (Marionette):** Marionette release 0.8.0 candidate — tag after CI
  green, cross-platform installer smoke, changelog consolidation for 0.7.40+.
- **Puppetmaster backlog (optional):** wire evaluator draft counts into
  dashboard cost rollup (cosmetic; no RQGM logic changes).
