# Design: persistent eval history

## Problem

Declarative check results are computed per worker run and surfaced once (in
the worker summary), then lost. There is no way to see "this check has failed
3 of the last 5 runs" or chart pass rate across a session. A small
session-scoped SQLite table (same pattern as the history-compaction journal
and spill registry) makes eval outcomes durable and cheap to aggregate.

## Shape

- New module `harness/eval_history.py` (stdlib only), SQLite db
  `eval_history.sqlite` in the session `state_dir`:

  ```sql
  CREATE TABLE IF NOT EXISTS evals (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      session_id TEXT NOT NULL,
      ts REAL NOT NULL,
      source TEXT NOT NULL,
      check_id TEXT NOT NULL,
      passed INTEGER NOT NULL,
      on_fail TEXT NOT NULL DEFAULT ''
  );
  ```

- `record_eval_results(state_dir, session_id, source, results)` where
  `results` is the list of check-result dicts already produced by
  `results_to_dicts`. Open/write/close per call (Windows file-lock lesson);
  all failures swallowed.
- `summarize_eval_history(state_dir, session_id=None) -> (recorded, failed)`.
- `eval_history_payload(state_dir, session_id) -> {"evals_recorded": N,
  "evals_failed": M}` mirroring `history_compaction_payload`.
- Recording hook: `harness/worker.py`, immediately after post checks run,
  records the full `check_payload` with `source="declarative_check"`. The
  worker has no session id, so the `session_id` column stores the
  Puppetmaster `job_id` (or "default") — the journal is state-dir-scoped
  either way, and per-job attribution is more useful at this seam.
  Auto-verify results are NOT recorded in v1 (that seam lives in
  `conversation.py` and would need result normalization; deferred).
- Surfacing: `_tool_output_savings_fields` in `harness/server.py` merges
  `eval_history_payload`, so both `/api/usage` and the swarm-live payload
  gain `evals_recorded` / `evals_failed`. UI: one CostBreakdown row
  "Checks recorded", shown only when nonzero.

## Integration map

| Piece | Location |
| --- | --- |
| Journal module | `harness/eval_history.py` (new) |
| Record hook | `harness/worker.py` post-check site |
| API fields | `harness/server.py` `_tool_output_savings_fields` |
| UI row | `webapp/src/components/CostBreakdown.tsx` |
| Tests | `tests/test_eval_history.py` (new) |

## Kill switch

`HARNESS_EVAL_HISTORY=0` disables recording (default on; recording is
read-only with respect to behavior).

## Non-goals (v1)

- Auto-verify outcome recording (deferred; see hook note above).
- Migrating existing journals/ledgers into this table.
- Trend charts (the UI shows counts; charting is a later wave).

## Acceptance

- Record/summarize round trip; failed write does not raise.
- `/api/usage` carries `evals_recorded` and `evals_failed`.
- Kill switch suppresses recording.
