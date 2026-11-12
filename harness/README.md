# harness: the product backend

The Python backend for the desktop app.
Stdlib HTTP + SSE server, the pilot loop, and the tools.
Puppetmaster is the kernel under it. The renderer lives in [`webapp/`](../webapp/README.md).

```
Electron renderer (React, three panes)
        |  window.harnessIPC  (IPC bridge: getJSON / postJSON / SSE / upload)
        v
stdlib Python backend  (harness/server.py)  --  SSE event stream
        |
   ConversationalSession  (harness/conversation.py)  -- the pilot loop
        |                         |                          |
  structured tools          CodeGraph context         Puppetmaster
  (read/search/edit/run)    (per-turn, self-heal)     Orchestrator(store).run(...)
                                                              |
                                                       durable SwarmStore
                                                       (jobs, artifacts, events)
```

## Start here

| Module | Role |
|---|---|
| `conversation.py` | The pilot loop facade: prose + structured tool calls, run them, feed the real results back. Mixins compose the session (`send_loop.py`, `busy_control.py`, `compaction_mixin.py`, and more). Yields events for the GUI. |
| `pilot.py` | The pilot contract: `PilotTurn` / `PilotAction` and the tool schema the providers see. New pilot tools start here. |
| `server.py` | Stdlib HTTP server shell streaming session events over SSE. Routes live in `api/`. |
| `api/` | HTTP route modules: sessions, settings, CodeGraph, wiki, jobs, economics, files, git, MCP, and more. |
| `command_policy.py` | Pure, stdlib-only: timeout resolution + danger classification for the full-auto guard. |
| `autobudget.py` | The AutoBudget governor for full-auto mode. |
| `wiki_orchestrator.py` | Local-model structuring of a session digest into wiki pages (PM-free, testable). |
| `state.py` | DurableState: a clean read layer over Puppetmaster's SwarmStore (jobs, artifacts, live events). |
| `config.py` | Swappable driver, reach, budget. |
| `vision.py` | Native pixels when the pilot can see, a VLM sidecar when it cannot. |

The full package map, with every mixin and plane, is in
[Architecture section 9](../docs/design/architecture.md#9-package-map).
Ownership rules for new code are in [AGENTS.md](../AGENTS.md).
