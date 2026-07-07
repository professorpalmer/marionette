# Marionette Harness -- Decisions & Fixes (v0.8.0 -> v0.9.x)

Continuation of [session_decisions_v0_7_12_to_24.md](session_decisions_v0_7_12_to_24.md).
Same conventions: source-run distribution; backend edits need restart; UI edits hot
via Vite HMR in dev. Puppetmaster ships as PyPI `puppetmaster-ai` (v1.10.0 as of
this slice).

## OMP token-efficiency lift (Round 10, v0.8.0)

Lifted high-ROI patterns from the MIT-licensed oh-my-pi reference without copying
their stack wholesale:

- **Absolute-token compaction advisor** (`harness/compaction_advisor.py`): advise
  compaction when context crosses both a percentage threshold and an absolute token
  floor, not percentage alone.
- **Savings-gated tool-output offload** (`harness/offload_policy.py`): spill large
  tool results to disk only when the compacted form saves enough tokens above a
  minimum payload size.
- **Per-turn output budget** (`harness/turn_budget.py`): user messages may carry
  `+Nk` (advisory) or `+Nk!` (hard) output token limits parsed into the pilot loop.

## Append-only context / KV-cache reuse (Round 11, v0.9.0)

For local and cache-discounting providers (DeepSeek-style), keep the prompt prefix
byte-stable across turns so provider-side KV caches reuse the heavy system prefix:

- `harness/append_only_context.py` resolves when append-only mode applies (driver
  settings + base URL heuristics).
- `harness/conversation.py` freezes the system prompt, moves dynamic turn context
  into a user-message trailer, and surfaces prefix-stability metrics in the usage
  payload.

## Swarm tracker accounting (v0.9.1+ on main)

The right pane Swarm Tracker had three connected bugs:

1. **Finished-header aggregate** showed a redundant tokens+cost total; removed --
   each card carries its own numbers; session totals live in the status bar only.
2. **Stale dispatch estimates**: job cost came from ROUTING pre-flight estimates and
   never updated. Fix: `_job_swarm_accounting` in `harness/server.py` prices usage
   via `puppetmaster.cost.price_job`, then tops up unpriced tasks from the live
   OpenRouter `/models` price map (same feed `pmharness.registry` already caches for
   context windows). Routing estimate is last resort only.
3. **Session total ignored swarm spend**: `/api/usage` and `/api/swarm/live` now add
   durable store-job dollars to `session.est_cost_usd` (local provider jobs were
   already folded via `_worker_cost_usd`).

Regression guard: when usage lands but neither registry nor live map can price the
model, keep the routing estimate instead of snapping finished jobs to $0.

## UI fixes

- **Double-posted assistant messages** (`webapp/src/components/Conversation.tsx`):
  reasoning/thinking rows inserted after a streaming bubble made `appendStreamingText`
  miss the active bubble and open a second one. `findStreamingBubbleIdx` scans past
  non-message items to find the still-streaming assistant row.
- **Windows path containment** (`harness/paths.py`): `realpath` on nonexistent paths
  can hang on Windows; resolve the longest existing ancestor then reattach the tail.
  Also expand 8.3 short names on CI runners so containment checks match long paths.

## Windows console flashes (cross-repo)

Background subprocesses must not pop visible consoles under console-less hosts:

- **Puppetmaster v1.10.0**: `puppetmaster/win_console.py` + early `hide_child_consoles()`
  in CLI, MCP server, worker runtime, codegraph index runner.
- **Portable LLM Wiki backend**: same hook in `app/win_console.py` at startup.
- **MarionetteWikiSync scheduled task**: Task Scheduler launched `powershell.exe`
  (console app) every 15 minutes even with `-WindowStyle Hidden`; repointed to
  `wscript.exe` + `sync-hidden.vbs` so git sync runs fully hidden.

Escape hatch: `PUPPETMASTER_SHOW_CONSOLES=1`.

## Releases shipped this slice

| Project | Tag | Highlights |
|---|---|---|
| Marionette | v0.9.0 | Append-only context, OMP R10 token lift |
| Marionette | v0.9.1 | Double-post fix, Windows hardening |
| Puppetmaster | v1.10.0 | MMR memory diversity, injection cost log, degraded agentic honesty, win_console |

Post-tag fixes on Marionette `main` (swarm accounting + live pricing) ship on the
next release cut; restart Marionette to pick them up before tagging.

## Docs

- README: expanded token economics, swarm cost transparency, append-only context,
  and the documentation index entry for this file.
