# Round 6 implementation plan (OMP medium-value leftovers, pick-list wave)

Instructions for the implementing model. Work the tasks IN ORDER. One commit
per task with the exact message given. Run the full suite
(`.\.venv\Scripts\python.exe -m pytest -q`) before every commit; run
`npm run build` in `webapp/` additionally when a task touches frontend files.
Push to main at the end and wait for CI green. Do NOT create any release tag.

Ground rules (same as rounds 1-5):

- No emojis anywhere. stdlib-only for the rig (`urllib`, `sqlite3`,
  `dataclasses`, `subprocess`). JSON, never YAML.
- SQLite writers open, write, and CLOSE per call (Windows file-lock lesson;
  copy `harness/spill_registry.py`).
- Hot-path hooks swallow their own failures; never wrap the caller's logic.
- Python 3.9 floor: no `match`, no `X | Y` unions in runtime annotations;
  keep `from __future__ import annotations` at module top.
- PowerShell shell: `;` separates commands, never `&&`.
- Subprocess capture always sets `encoding="utf-8", errors="replace"` —
  never rely on the platform default codec (Windows cp1252 lesson from
  round 4 hardening).
- CI now runs the suite on Linux, macOS, AND Windows (3.9 floor + 3.11 where
  the runner supports it). A change is not done until all legs are green.
  Tests must not assume POSIX shells, `/tmp` semantics, or forgiving
  `TemporaryDirectory` teardown (see the round-5 fix in
  `tests/test_agent_tools.py` — use `mkdtemp` + `shutil.rmtree(...,
  ignore_errors=True)` when a session may still hold handles at exit).

Context: Waves 1-4 shipped the OMP tranche (tool discovery, internal URIs,
hash edits, LSP diagnostics, savings ledger), memory offload (`spill://`),
provider cassettes, and declarative checks v1-v2 (the full Shepherd arc).
Wave 5 shipped the RQGM evaluator lifts in Puppetmaster (v1.5.x). This wave
closes out the OMP backlog: the four medium-value leftovers, shipped as a
pick-list — **each item ships only after its design note is written and
committed**. If an item's design uncovers a blocker or the cost/benefit turns
bad, drop it and record why in the design note rather than forcing it.

## Pick-list overview (work in this order)

| Item | Value | One-line scope |
| --- | --- | --- |
| A. Time-travel rules | Medium | Replay a session's rule/config state as of an earlier turn so a regression can be reproduced against the exact context that produced it. |
| B. Advisor | Medium | A read-only "second opinion" pass the pilot can request: cheap model reviews the pending action list before execution, returns warnings only (never blocks). |
| C. AST preview | Medium | Before applying a hash edit to a Python file, parse both versions with `ast` and surface a structural diff (functions/classes added/removed/changed) in the edit confirmation payload. |
| D. Persistent eval | Medium | Persist per-session eval outcomes (declarative check results, verify passes/fails) into a session-scoped SQLite table so `/api/usage` and the UI can show trend lines across turns instead of last-result-only. |

## Task A0 (mandatory first): design notes

1. For each item you intend to ship, write
   `artifacts/design_time_travel_rules.md`, `artifacts/design_advisor.md`,
   `artifacts/design_ast_preview.md`, `artifacts/design_persistent_eval.md`
   following the structure of `artifacts/design_declarative_checks.md`
   (Problem / Shape / Integration map / Non-goals / Acceptance).
2. Each note MUST name: the module(s) it touches, the kill-switch env var
   (every feature ships default-safe: advisor and AST preview default OFF,
   time-travel and persistent eval default ON but read-only), and the test
   file it will land in.
3. An item without a committed design note does not ship. Dropping an item is
   allowed — say so in a short note (`artifacts/design_<item>_dropped.md`)
   with the reason.
4. Commit: "Add round 6 design notes for OMP leftover items"

## Task A: time-travel rules

1. Read first: how session state snapshots are stored (`harness/server.py`
   session endpoints, `state_dir` layout) and where per-turn config is
   resolved (`HarnessConfig.from_env`, conversation turn loop).
2. Record: at each turn boundary, append a compact JSON line (turn index,
   ts, active rule/check spec hashes, relevant env toggles) to
   `{state_dir}/turn_context.jsonl`. Append-only, open/write/close per call,
   failures swallowed.
3. Replay: `GET /api/session/context_at?turn=N` returns the recorded snapshot
   for turn N (404 when absent). No behavior changes on the live session —
   v1 is observability only.
4. Tests: record + read back across 3 turns; malformed lines skipped;
   missing file yields empty.
5. Commit: "Add turn-context journal for time-travel rule inspection"

## Task B: advisor

1. Read first: where the pilot's proposed actions are parsed before execution
   in `harness/conversation.py` (the action list loop) and how a second
   model call is made cheaply (`Driver.complete` with a system prompt).
2. `harness/advisor.py`: `advise(actions, repo, driver) -> list[str]`
   returning zero or more warning strings. Prompt is a single fixed template
   listing the pending actions; response parsed as a JSON array of strings,
   anything unparseable becomes zero warnings. Hard cap one advisor call per
   turn; total failure yields zero warnings.
3. Gate on `HARNESS_ADVISOR=1` (default off). When warnings return, prepend
   them to the action_result event data as `advisor_warnings` — advisory
   only, execution proceeds regardless.
4. Tests: disabled by default; enabled path with a fake driver returning
   warnings; garbage response yields no warnings and no exception.
5. Commit: "Add opt-in advisor pass for pending pilot actions"

## Task C: AST preview

1. Read first: `harness/hash_edits.py` (or wherever the hash-edit apply path
   lives — find it via the round-1 OMP commit) and the payload returned to
   the UI on a pending edit.
2. `harness/ast_preview.py`: `structural_diff(before: str, after: str) ->
   dict` using stdlib `ast` — top-level and nested function/class names
   added/removed/signature-changed. Non-Python or unparseable sources return
   `{"available": False}` — never raise.
3. Wire into the hash-edit path behind `HARNESS_AST_PREVIEW=1` (default
   off): merge the diff dict into the edit payload as `ast_preview`.
4. Tests: add/remove/rename function detected; syntax-error input safe;
   disabled by default.
5. Commit: "Add opt-in AST structural preview for hash edits"

## Task D: persistent eval

1. Read first: how declarative check results flow post-run
   (`harness/worker.py` post-check site, `harness/conversation.py` summary
   append) and the spill-registry SQLite pattern.
2. `harness/eval_history.py`: SQLite db `eval_history.sqlite` in the session
   `state_dir`, one table
   `evals(id INTEGER PRIMARY KEY, session_id TEXT, ts REAL, source TEXT,
   check_id TEXT, passed INTEGER, on_fail TEXT)`. `record_eval_results(...)`
   and `summarize_eval_history(state_dir, session_id=None)` mirroring the
   history-compaction journal API. All writes swallowed on failure.
3. Hook: after post checks run in `harness/worker.py`, record results
   (source `"declarative_check"`). Auto-verify results may be recorded from
   the same seam if trivially reachable; otherwise leave for a later wave —
   say which you did in the design note.
4. Surface: `GET /api/usage` gains `evals_recorded` and `evals_failed`
   counts, mirroring `_history_compaction_fields()`. UI: one CostBreakdown
   row "Checks recorded", shown only when nonzero (copy the history
   compaction row pattern).
5. Tests: record/summarize round trip; failed write does not raise;
   usage fields present.
6. Commit: "Persist declarative check outcomes to session eval history"

## Task E: release prep (bump, no tag)

1. Bump to `0.7.46` in the same four files as the `0.7.45` bump
   (`pyproject.toml`, `harness/__init__.py`, `webapp/package.json`,
   `webapp/package-lock.json`).
2. Full verification: `python -m pytest -q` green, `npm run build` green.
3. Commit: "chore(release): bump version to 0.7.46", push, wait for CI green
   on ALL OS legs (`gh run watch <id> --exit-status`). Report run id.
4. NO TAG — the user cuts tags after review.

## Verification (after ALL tasks)

```powershell
cd C:\Users\pwall\Projects\marionette
.\.venv\Scripts\python.exe -m pytest -q
cd webapp; npm run build; cd ..
git push origin main
# gh run watch <run-id> --exit-status  (all OS legs green)
# NO TAG.
```

## Out of scope (do not do)

- Puppetmaster repo changes (Wave 7+ owns evaluator-aware review gate and
  dashboard lineage).
- Time-travel *enforcement* (replaying old rules against new turns) — v1 is
  inspection only.
- Advisor blocking or auto-rewriting actions.
- AST preview for non-Python languages.
- Migrating existing journals/ledgers into the eval-history table.
- YAML anywhere. Pushing any tag.

## After Wave 6 (roadmap)

- **Wave 7 (Puppetmaster):** evaluator-aware review gate (read epoch snapshot
  criteria instead of the hardcoded judge prompt), dashboard surfacing of
  evaluator slot lineage, optional Redis/Postgres registry backend.
- **Wave 8 (either):** TencentDB-style L0-L3 memory layering study — design
  note first, building on spill offload measurements.
