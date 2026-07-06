# OMP Feature Patch — Ship Plan (implementer handoff)

Audience: an implementation agent working in `C:\Users\pwall\Projects\marionette`
(Python backend in `harness/`, tests in `tests/`, React frontend in `webapp/`).
Scope: finish and ship the oh-my-pi (OMP) feature patch that already lives in the
dirty working tree. Do NOT start any Shepherd / Tencent / RQGM work — those are
explicitly deferred to post-ship.

Repo conventions that MUST be honored:
- No emojis anywhere (code, comments, commits, docs).
- Windows dev box; shell is PowerShell (`;` separates commands, never `&&`).
- All tests must pass offline: `python -m pytest -q` from the repo root using the
  project venv. Frontend check: `npm run build` inside `webapp/`.
- Release gate: CI must be green on Python 3.9, 3.11, and frontend-build BEFORE
  any tag. Local green does not substitute.
- New files that write JSON must use LF newlines (`newline='\n'`) and secure
  permissions via `harness/secure_files.py:restrict_to_owner` where applicable
  (not relevant to the tasks below, but do not regress it).

## Background: what is already done (do not redo)

Five OMP features are implemented in the working tree:
1. Tool discovery (`harness/tool_discovery.py`, `ToolCatalog`) — reduces the
   visible tool schema; default ON via `HARNESS_TOOL_DISCOVERY` (off only when
   set to `0`).
2. Tool-output savings ledger (`harness/tool_output_savings.py`) — fully wired
   through `context_budget.py`, `conversation.py`, `server.py`, and the webapp
   StatusBar/CostBreakdown/SwarmPane. Complete; leave alone.
3. Internal URI reads (`harness/internal_uri.py`) — `job://`, `artifact://`,
   `agent://`, `conflict://` resolved in `tool_dispatch._do_read_file` /
   `_do_list_dir`. Complete except Task 3 below.
4. Hash-anchored edits (`harness/hash_edit.py`) — opt-in via `HARNESS_HASH_EDIT=1`,
   default off. Complete; ships as opt-in. Do not change the default.
5. LSP diagnostics tool (`harness/lsp_code_intelligence.py`) — pyright/tsc
   diagnostics, wired through pilot schema, `_do_lsp`, and a conversation-loop
   branch. Complete.

Full local suite is green (1104 passed, 70 skipped). The remaining work is three
small code fixes, tests, and an atomic commit + CI gate.

## Task 1 (BLOCKER): make the chat hot path use the tool catalog

File: `harness/conversation.py`, inside `ConversationalSession` — the streaming
turn loop around lines 2265-2290.

Problem: the hot path builds the FULL tool schema, bypassing tool discovery:

- Line ~2271: `mcp_section = _format_mcp_tools_section(self._mcp)` — legacy
  call. `get_context_usage()` (line ~979) shows the correct call shape:
  `_format_mcp_tools_section(self._mcp, self._tool_catalog, no_delegation=...,
  browser_enabled=...)`. Change the hot-path call to match exactly.
- Lines ~2283-2286: inside `if hasattr(self.pilot, "chat"):` it imports
  `build_tools_schema` from `.pilot` and calls it with the full MCP tool list.
  Replace those lines with a single call:
  `tools_schema = self._build_visible_tools_schema()`
  (defined at line ~928; it already handles mcp_tools, no_delegation, and
  browser_enabled internally). Remove the now-unused
  `from .pilot import build_tools_schema` import and the `mcp_tools = ...`
  line IF nothing else in that scope uses them (verify before deleting).

Behavioral note: when `HARNESS_TOOL_DISCOVERY=0`, `ToolCatalog.visible_schema()`
must return the same full schema as before — confirm by reading
`harness/tool_discovery.py:visible_schema` and its tests; if a test for this
parity does not exist, add one (see Task 4).

## Task 2 (BLOCKER): add the `search_tools` execution branch

File: `harness/conversation.py`, in the per-action execution section of the
turn loop (the long chain of `if act.kind == ...: ... continue` blocks around
lines 3091-3158).

Problem: `search_tools` gets an `action_start` label (line ~2643) but no
execution branch, so the dispatcher `ToolDispatchMixin._do_search_tools`
(`harness/tool_dispatch.py` line 449) is never called. The model sees the
action start and never gets a result.

Fix: add a branch modeled EXACTLY on the `lsp` branch (lines 3140-3158). Place
it adjacent to the `search_files` branch:

```python
# ---- search_tools branch ---------------------------------------
if act.kind == "search_tools":
    try:
        ok, status, val = self._do_search_tools(act)
    except Exception as exc:
        ok, status, val = False, "exception", str(exc)

    if ok:
        yield ConvEvent("action_result", {
            "id": aid, "num": 1, "types": ["search_tools"], "adapter": "local", "mode": "tool",
            "artifacts": [{"type": "search_tools", "headline": f"Tool search: {act.query or 'activate'}"}],
        })
        self._append_action_result(act, aid, f"(search_tools returned)\n{val}", is_native)
    else:
        yield ConvEvent("action_result", {"id": aid, "error": val})
        self._append_action_result(act, aid, f"(search_tools failed: {val})", is_native)
    continue
```

Notes:
- Read `_do_search_tools` in `tool_dispatch.py` first to confirm the return
  tuple shape `(ok, status, val)` and what `val` contains on success (it is a
  string payload; append it verbatim like the lsp branch does).
- Do NOT add `search_tools` to the prefetch section unless the other read-only
  search branches share prefetch plumbing and it is trivial — correctness over
  cleverness. The non-prefetch direct call above is sufficient.
- Activation side effects (the catalog marking tools visible) happen inside
  `_do_search_tools` via `self._tool_catalog`; after activation the NEXT turn's
  `_build_visible_tools_schema()` (Task 1) will include the newly activated
  tools. That is the whole point — Task 1 and Task 2 only work together.

## Task 3: fix `_internal_uri_context` to use the session state dir

File: `harness/tool_dispatch.py`, lines 52-56.

Problem: it passes `self.config.state_dir or ""`, but `ConversationalSession`
resolves its real state dir at line ~325 of `conversation.py`
(`self.state_dir = config.state_dir or tempfile.mkdtemp(...)`). When the config
value is blank, internal URI reads point at "" instead of the temp dir.

Fix:

```python
state_dir=getattr(self, "state_dir", None) or self.config.state_dir or "",
```

## Task 4: tests

1. `tests/test_tool_dispatch_mixin.py` — add `_do_search_tools` and
   `_do_hash_edit` to `MOVED_METHODS` (pattern already exists in the file; `_do_lsp`
   was added the same way).
2. New conversation-loop integration test (put in `tests/test_tool_discovery.py`
   or a new `tests/test_search_tools_loop.py`, matching whichever style is
   closer): drive `ConversationalSession` with a fake pilot whose first response
   issues a `search_tools` action (query for a hidden tool, e.g. "lsp") and
   assert that (a) an `action_result` event without `error` is yielded, (b) the
   appended tool result contains the search payload, and (c) after activation
   the schema from `_build_visible_tools_schema()` includes the activated tool.
   Study existing loop tests (search the tests dir for `ConvEvent` and fake
   pilots) and copy their fixture style — do not invent new harness scaffolding.
3. Schema-parity test: with `HARNESS_TOOL_DISCOVERY=0` (use monkeypatch of
   env), `_build_visible_tools_schema()` equals `build_tools_schema(...)` for
   the same inputs. Skip if an equivalent test already exists.

## Task 5: verification gauntlet (all must pass before committing)

```powershell
# from repo root, using the project venv python
python -m pytest -q
```

Then frontend:

```powershell
# in webapp/
npm run build
```

Both must be fully green. If the suite hangs or a Windows file-lock error
appears in ledger tests, do not paper over it — report it.

## Task 6: atomic commit

The OMP patch is split across 11 modified tracked files AND these untracked
files, which MUST be staged in the same commit or main breaks on import:

- `harness/hash_edit.py`
- `harness/internal_uri.py`
- `harness/lsp_code_intelligence.py`
- `harness/tool_discovery.py`
- `harness/tool_output_savings.py`
- `tests/test_hash_edit.py`
- `tests/test_internal_uri.py`
- `tests/test_lsp_code_intelligence.py`
- `tests/test_tool_discovery.py`
- `tests/test_tool_output_savings.py`
- `artifacts/` acceptance docs (include; they are the feature acceptance record)

Do NOT commit anything under `results/` (repo rule: never commit
`results/*.sqlite`). Check `git status` for strays before staging.

Single commit, plain-words message, for example:
"Integrate OMP lifts: tool discovery, hash edits, internal URIs, LSP diagnostics, tool-output savings ledger"

Push to main, then WAIT for CI (Python 3.9 + 3.11 + frontend-build) to go
green before any release/tag step. Do not tag; the release cut is a separate
follow-up owned by the user.

## Explicitly out of scope (do not do)

- Exposing `search_internal_uris` as a pilot tool (post-ship).
- Changing the `HARNESS_HASH_EDIT` default (stays opt-in).
- LSP references/rename support or an LSP acceptance doc (post-ship).
- Any Shepherd / Tencent / RQGM feature work.
- History-compaction journal and per-job swarm compaction attribution.
- Any refactor beyond the exact lines named above.
