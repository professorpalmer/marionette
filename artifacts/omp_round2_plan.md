# Round 2 Plan — OMP Post-Ship Items + First Research Lifts (implementer handoff)

Audience: an implementation agent working in `C:\Users\pwall\Projects\marionette`
(Python backend in `harness/`, tests in `tests/`, React frontend in `webapp/`).
Prerequisite: the OMP integration commit (2862cd9) and the v0.7.44 release are
on main with green CI. This plan covers everything the round-1 plan explicitly
deferred, in priority order. Implement the tasks IN ORDER; each task is
independently committable and must leave the suite green.

Repo conventions that MUST be honored (same as round 1):
- No emojis anywhere (code, comments, commits, docs).
- Windows dev box; shell is PowerShell (`;` separates commands, never `&&`).
- Verify with `python -m pytest -q` (project venv, repo root) and
  `npm run build` in `webapp/` after frontend-touching tasks.
- Release gate: CI green on Python 3.9 + 3.11 + frontend-build before any tag.
  Python floor is 3.9 — no 3.10+ syntax (no `match`, no `X | Y` type unions in
  runtime code; `from __future__ import annotations` is fine and already used).
- LF newlines for new JSON writers; `restrict_to_owner` for credential-like files.
- One commit per task, plain-words message. Do NOT tag; releases are cut
  separately by the user.

Codebase orientation (read these before editing):
- `harness/pilot.py` — `VALID_ACTION_KINDS` (line ~40), `PilotAction` dataclass
  with per-kind validation (~line 144), `build_tools_schema(...)` (~line 215).
- `harness/conversation.py` — `ConversationalSession`; the per-action execution
  loop is a chain of `if act.kind == ...: ... continue` blocks (~lines 3090-3260);
  action goal labeling is the `elif act.kind == ...` chain (~line 2624).
- `harness/tool_dispatch.py` — `ToolDispatchMixin` with `_do_*` handlers;
  `tests/test_tool_dispatch_mixin.py:MOVED_METHODS` must list every handler.
- `harness/tool_discovery.py` — `ToolCatalog`, `CORE_ALWAYS`, `_PILOT_EXTRAS`,
  `core_visible_names()`.
- `harness/internal_uri.py` — `search_internal_uris(query, ctx, *, scheme,
  max_results)` already exists and is unit-tested (returns a plain string of
  tab-separated hits, one per line).
- `harness/context_budget.py` — `maybe_persist_result`, `enforce_turn_budget`,
  `_notify_compaction`, `CompactionCallback`.
- `harness/tool_output_savings.py` — savings ledger (SQLite + optional JSONL).

---

## Task A: expose `search_internal_uris` as a pilot tool

Goal: agents can keyword-search durable state (jobs, artifacts, agent
transcripts, conflicts) instead of needing to already know a `job://` URI.

1. `harness/pilot.py`:
   - Add `"search_state"` to `VALID_ACTION_KINDS`. (Name it `search_state`,
     NOT `search_internal_uris` — tool names are for the model; keep it short
     and intent-revealing. The internal function name stays as is.)
   - In `PilotAction.validate` (the chain at ~line 144) add:
     `search_state` requires a non-empty `query`.
   - In `build_tools_schema`, add a schema entry next to `search_files`:
     name `search_state`, description along the lines of: "Search durable
     state (past jobs, artifacts, agent transcripts, merge conflicts) by
     keyword. Returns matching internal URIs (job://, artifact://, agent://,
     conflict://) that can be opened with read_file or list_dir." Parameters:
     `query` (string, required), `scheme` (string, optional, one of
     job/artifact/agent/conflict), `max_results` (integer, optional, default 50).
   - Confirm how `parse_tool_calls` maps native calls to kinds (it keys off the
     function name); `search_state` should parse into `kind="search_state"`
     with `query` populated the same way `search_files` does. Follow the
     existing pattern exactly.
2. `harness/tool_dispatch.py`: add handler

```python
def _do_search_state(self, act: Any) -> tuple[bool, str, str]:
    from .internal_uri import search_internal_uris

    args = act.arguments or {}
    query = (act.query or args.get("query") or "").strip()
    if not query:
        return False, "invalid_arguments", "search_state requires a 'query'"
    scheme = args.get("scheme") or None
    max_results = args.get("max_results", 50)
    try:
        text = search_internal_uris(
            query,
            self._internal_uri_context(),
            scheme=scheme,
            max_results=max_results,
        )
        return True, "success", text
    except Exception as e:
        return False, "exception", str(e)
```

3. `harness/conversation.py`:
   - Goal labeling: add `elif act.kind == "search_state": act_goal = act.query`
     in the labeling chain (~line 2641 area).
   - Execution branch: add next to the `search_tools` branch (~line 3143),
     modeled on it exactly (try/except around `self._do_search_state(act)`,
     success emits `types: ["search_state"]` with headline
     `f"State search: {act.query}"`, result appended as
     `f"(search_state returned)\n{val}"`).
4. Tool discovery visibility: `search_state` should be discoverable but NOT
   core-visible. Do NOT add it to `CORE_ALWAYS` or `_PILOT_EXTRAS` in
   `tool_discovery.py`. Verify the catalog picks up new built-ins automatically
   from `build_tools_schema` (read `ToolCatalog.refresh` to confirm); if
   builtin entries are derived from `build_tools_schema(include_search_tools=True)`
   output, no catalog change is needed.
5. Tests:
   - `tests/test_tool_dispatch_mixin.py`: add `_do_search_state` to `MOVED_METHODS`.
   - In `tests/test_internal_uri.py` (or a new focused file): a dispatch test
     that seeds a temp state dir with one job + one artifact (copy the seeding
     helpers already used by the existing `search_internal_uris` unit tests),
     builds a session/mixin the way `test_search_tools_handler_on_session`
     does in `tests/test_tool_discovery.py`, calls `_do_search_state`, and
     asserts a `job://` hit is returned.
   - A discovery test: with discovery ON, `search_state` is hidden by default
     and appears in `_build_visible_tools_schema()` after
     `catalog.activate(["search_state"])`.
6. Commit: "Add search_state pilot tool for keyword search over durable state URIs"

## Task B: LSP acceptance doc + references support

Two halves; the doc is mandatory, references support is the feature.

1. References support in `harness/lsp_code_intelligence.py`:
   - Current state: diagnostics-only via pyright CLI / `tsc --noEmit`. Add a
     `references` mode with a deliberately modest scope: locating usages of a
     symbol name across the repo using the LSP tools when available.
   - Pyright supports this poorly from the CLI; do NOT shell out to a long-lived
     LSP server (out of scope — too heavy). Instead implement `references` as a
     hybrid: use CodeGraph when present (`_puppetmaster_cmd` passthrough is
     already the pattern in `tool_dispatch.py` — read `_do_search_codegraph`)
     and fall back to a plain word-boundary text scan (reuse `_do_search_files`
     machinery) when CodeGraph is unavailable. Return the same formatted-text
     shape as diagnostics mode.
   - Wire the new mode through `get_lsp_report(mode="references", symbol=...)`;
     the `lsp` tool schema in `pilot.py` gains a `symbol` parameter
     (string, required when mode is `references`).
   - Do NOT attempt rename support. Rename is edit-producing and belongs to a
     future slice with checkpoint integration; note it in the acceptance doc as
     deferred.
2. Acceptance doc `artifacts/lsp_acceptance.md`, following the exact structure
   of `artifacts/hash_edit_acceptance.md`: what shipped, how it is gated, what
   was verified (name the tests), what is explicitly deferred (rename,
   long-lived LSP servers, non-Python/TS languages).
3. Tests in `tests/test_lsp_code_intelligence.py`: references mode returns hits
   for a symbol defined in a temp repo fixture; references mode degrades
   gracefully (no exception, informative message) when neither CodeGraph nor
   the fallback finds anything; schema test that `lsp` accepts `symbol`.
4. Commit: "Add LSP references mode and acceptance doc"

## Task C: history-compaction journal (snapcompact-style)

Goal: when the conversation history itself is compressed (summarization of old
turns), record what was dropped and the token delta in a durable, inspectable
journal — same spirit as the tool-output savings ledger, but for history.

1. Locate the history summarization path first: search `conversation.py` for
   `_compressed_summary` (used in `get_context_usage`, ~line 998) and find the
   function that sets it (the compaction/summarization routine and its call
   site). Read it fully before coding. The journal hooks in exactly there.
2. New module `harness/history_compaction_journal.py` (stdlib only), mirroring
   the layering of `tool_output_savings.py`:
   - SQLite db `history_compaction.sqlite` in the session `state_dir` with one
     table: `compactions(id INTEGER PRIMARY KEY, session_id TEXT, ts REAL,
     messages_compacted INTEGER, chars_before INTEGER, chars_after INTEGER,
     summary_preview TEXT)`. `summary_preview` is the first 400 chars of the
     replacement summary.
   - `record_history_compaction(state_dir, session_id, messages_compacted,
     chars_before, chars_after, summary_preview)` — open, write, CLOSE the
     connection every call (Windows file-lock lesson from round 1: the ledger
     must never hold the SQLite handle open, or TemporaryDirectory cleanup
     fails with WinError 32).
   - `summarize_history_compactions(state_dir, session_id=None)` returning a
     small dataclass (count, chars_before, chars_after, tokens_saved via the
     same `CHARS_PER_TOKEN` heuristic — import it from `tool_output_savings`).
   - Everything wrapped so failure to journal NEVER breaks compaction
     (try/except pass around the write, like `_notify_compaction`).
3. Hook the summarization routine in `conversation.py`: after it replaces a
   block of messages with a summary, call `record_history_compaction` with the
   before/after sizes. Use `self.state_dir` and
   `self.harness_session_id or "default"` exactly like the tool-output ledger.
4. Surface: extend `_tool_output_savings_fields()` (or add a sibling
   `_history_compaction_fields()` merged in the same `get_context_usage`
   return) with `history_compactions` (count) and
   `history_tokens_saved`. Then in `harness/server.py`, find where
   `_tool_output_savings_fields`-derived values flow into `/api/usage` and add
   the two new fields the same way.
5. Frontend (small): in `webapp/src/lib/api.ts` extend the usage type with the
   two fields; in `webapp/src/components/CostBreakdown.tsx` add one row
   "History compaction" shown only when `history_compactions > 0`, styled
   identically to the existing tool-output savings row.
6. Tests: new `tests/test_history_compaction_journal.py` — record/summarize
   round-trip in a TemporaryDirectory (this implicitly proves the handle is
   closed on Windows); a conversation-level test that forces the summarization
   path (find how existing compaction tests in `tests/test_compaction.py`
   trigger it and extend there if more natural) and asserts a journal row lands.
7. Commit: "Add history-compaction journal with usage API and UI surfacing"

## Task D: per-job swarm compaction attribution

Goal: the savings ledger currently records against a session id; swarm workers'
compactions should also carry which Puppetmaster job they belonged to, so the
SwarmPane can show per-job savings.

1. Read first: `harness/tool_output_savings.py` (`ToolOutputSavingsLedger`,
   `make_compaction_callback`, `session_savings_payload`), and in
   `harness/server.py` the `/api/swarm/live` handler (search for
   `_tool_output_savings_fields` and `swarm/live`).
2. Schema: add a nullable `job_id TEXT` column to the ledger table. The module
   creates its schema on first open; add a lightweight migration: on open,
   `PRAGMA table_info(...)` and `ALTER TABLE ... ADD COLUMN job_id TEXT` if
   missing. Keep it inside the module, tested.
3. Plumb: `make_compaction_callback(..., job_id: Optional[str] = None)` stores
   it. Callers: worker sessions know their job id — find where worker
   conversations are constructed (search `conversation.py` / `worker.py` for
   how `no_delegation` workers get config) and pass the job id through the
   same path `savings_session_id` travels in `enforce_turn_budget`
   (`harness/context_budget.py` ~line 268 builds the per-message callback —
   extend it with an optional `savings_job_id`).
4. Summarize: `summarize(session_id=..., job_id=...)` filter; add
   `job_savings_payload(state_dir, job_id)` returning the same shape as
   `session_savings_payload`.
5. Surface: in `server.py`'s `/api/swarm/live` payload, add per-job
   `tool_output_tokens_saved` using `job_savings_payload`. In
   `webapp/src/components/SwarmPane.tsx`, append the savings to the existing
   aggregated cost line when nonzero (the `jobCost`/`formatCost` code added
   recently is the anchor — keep the display consistent with it).
6. Tests in `tests/test_tool_output_savings.py`: migration adds the column to
   a pre-existing db created without it; records with job_id filter correctly;
   `job_savings_payload` shape.
7. Commit: "Attribute tool-output savings to swarm jobs and surface per-job totals"

## Task E: hash_edit default decision (config, not code-default flip)

The round-1 constraint stands: `HARNESS_HASH_EDIT` stays default-off in code.
What ships now is user-facing control:

1. `harness/server.py`: find `_persist_env_setting` / `_load_env_settings` and
   the `/api/settings` handler; add `hash_edit_enabled` as a persisted boolean
   setting that writes `HARNESS_HASH_EDIT=1|0` through the same mechanism the
   other env-backed settings use (mirror an existing boolean setting
   end-to-end — do not invent a new pattern).
2. `webapp` settings page: add a toggle "Hash-anchored edits (experimental)"
   in the same section as other experimental/behavior toggles, defaulting to
   off, wired to the settings API like its neighbors.
3. Test: settings round-trip test alongside the existing env-settings tests in
   the server test file (search `tests/` for `_persist_env_setting` usage).
4. Commit: "Expose hash_edit as a persisted settings toggle"

## Task F (research lifts, design-first — do NOT free-code these)

The first Shepherd/Tencent tranche. Each of these REQUIRES a short design note
in `artifacts/` reviewed by the user before implementation, because they touch
kernel behavior. For this round, produce the design notes only:

1. `artifacts/design_declarative_checks.md` — Shepherd-style declarative
   pre/post checks for worker tasks (existing anchor: `harness/verify.py` and
   the verification artifacts Puppetmaster emits). Cover: check spec format,
   where specs live, enforcement point, failure phase taxonomy.
2. `artifacts/design_provider_cassettes.md` — record/replay layer for provider
   calls (anchor: `pmharness/drivers/`, the offline test suite). Cover: cassette
   format, capture toggle, replay determinism, secret scrubbing.
3. `artifacts/design_memory_offload.md` — Tencent-style verbose-output offload
   to durable refs (anchor: `maybe_persist_result` already spills to disk;
   the design extends it to internal URIs so spilled outputs become
   `artifact://`-addressable). Cover: URI scheme, retention, interaction with
   the savings ledger.

Each note: one page, problem, proposed shape, integration points with exact
file/function names, test strategy, explicit non-goals. No code changes in
this task. Commit: "Add design notes for declarative checks, provider cassettes, memory offload"

---

## Ordering and verification recap

Implement A through F in order. After EACH task: full `python -m pytest -q`
green; `npm run build` green when webapp was touched; one commit. After F,
push main and confirm the tests workflow is green on 3.9 + 3.11 +
frontend-build. Do not tag.

## Explicitly out of scope for round 2

- LSP rename support and long-lived LSP server processes.
- Flipping the `HARNESS_HASH_EDIT` code default.
- Implementing (as opposed to designing) declarative checks, cassettes, or
  memory offload. Any other Shepherd/Tencent/RQGM implementation work.
- Fork/merge/discard scopes, content-addressed execution cache, effect-surface
  permissions (Shepherd "adopt later" tier).
- Evaluator evolution / RQGM concepts.
- Any refactor beyond the integration points named above.
