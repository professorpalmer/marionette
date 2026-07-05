# Marionette Harness -- Architecture Decisions (v0.7.7 -> v0.7.15)

Durable record of decisions, fixes, and rationale from a heavy iteration session.
Marionette runs from source; a "release" is just `main` moving forward (users pull
via the in-app Update & Relaunch pill). No DMG/asar release path.

## Distribution model (critical for where fixes belong)
- Backend runs from the source checkout (`python -m harness.cli`); edits to
  `harness/**` and `pmharness/**` go live on a BACKEND RESTART.
- Renderer UI is `webapp/src/**` (React). With "Live UI (Vite HMR)" on, UI edits
  hot-reload instantly with no rebuild/restart. Otherwise `npm run build` + relaunch.
- Puppetmaster is the one runtime dependency, installed from PyPI as
  `puppetmaster-ai` (`scripts/install.sh`). Only the author has it editable from a
  local checkout. THEREFORE: fixes to worker/adapter behavior must live in
  Marionette's `pmharness/bridge.py` (ships with Marionette, version-independent),
  NOT upstream in Puppetmaster -- an upstream fix needs a new PyPI release + a
  version pin, adding onboarding friction for new users.
- Two frontends exist: the shipping React app (`webapp/src/`, entry
  `Conversation.tsx`) and a dead legacy stdlib GUI (`harness/web/app.js`). All UI
  work targets `webapp/src/`.

## State & persistence
- Stable state dir: with no `HARNESS_STATE_DIR`, `server.py` anchors state to
  `~/.pmharness/state` (was per-run mkdtemp -> transcripts/sessions were lost on
  reload). Transcripts persist at `{state_dir}/transcripts/{session_id}.json`;
  session list at `harness_sessions.json`.
- Prompt queue persists across restart (`prompt_queue.json`, atomic writes, mirrors
  swarm_local_jobs). Save reads a snapshot under the lock then writes OUTSIDE it
  (lock is non-reentrant) to avoid deadlock.
- Wiki config (`wiki.json`) resolution: falls back to legacy `~/.pmharness/wiki.json`
  when the state-dir copy is absent (the stable-state-dir move orphaned it).
- Session token counters (`_tokens_cached`, `_tokens_used`, `_tokens_in/out`) are
  LIVE per-process accumulators, NOT persisted in the transcript. They reset to 0 on
  backend restart -- intentional (per-session, not lifetime). Decided to keep as-is.

## Token economics (the core competitive parity work)
- Prompt caching: multi-provider cache_control (Anthropic explicit breakpoints,
  OpenAI/Gemini implicit). Anthropic driver uses TWO history breakpoints -- a stable
  one on the second-to-last message (reused as a cache READ next turn) plus a moving
  one on the last message (extends the cached prefix). Stays within Anthropic's
  4-marker limit (system + last-tool + 2 history).
- Cost accounting: cached tokens billed at CACHE_READ_MULTIPLIER = 0.1 (not full
  input price). `_session_cost`/`_cache_savings`/`_job_cost` helpers in server.py.
  `/api/usage` surfaces `tokens_cached` + `cache_savings_usd`; StatusBar shows the
  dollar savings. Job cost uses the real input/output split (was a wrong 50/50 blend).
- Context meter: `_estimate_context_tokens` prefers the driver's REAL last prompt
  token count (`_last_prompt_tokens`) with `max(real, chars//4 heuristic)` so the
  75% compaction trigger and composer % track billed context, never under-counting.
- A big "N cached (~$X saved)" with tiny live cost is CORRECT on long sessions: the
  system prompt + tool schema + growing history are re-read from cache every turn;
  over many turns cache reads reach millions. Formula: 0.9 * cached/1e6 * price_in.

## Security
- Centralized GET auth gate: `do_POST` had one token gate but `do_GET` required each
  handler to re-add a copy-pasted check -- ~11 endpoints (/api/memory, /api/config,
  /api/skills, /api/rules, /api/commands, /api/settings, /api/platform, /api/jobs,
  /api/workspace, /api/mcp*) were UNAUTHENTICATED and leaked durable memory/config.
  Now a single gate at the top of `do_GET` authenticates every non-public path;
  public allowlist is only the renderer bootstrap assets (/, index.html, app.js,
  app.css). Regression test locks it in.
- Side effect caught by tests: `serve()`'s reuse-detection probed /api/config with
  NO token, so the gate made it read a live server as dead and try to double-bind.
  Fixed: any HTTP response (incl. 403) proves a server is running.

## Swarm / findings reliability
- Fan-out: `infer_roles` (pure, in `pmharness/intent.py`) broadened so common broad
  goals ("look through the codebase for bugs", "how does the X system work") fan out
  to multiple workers instead of a lone explorer; pinpoint lookups ("callers of X",
  "where is X") stay a single worker (pinpoint precedence beats broad keywords).
- Findings capture: `_compact_artifact` reads headline text from many payload keys
  (claim/decision/risk/report/message/text/stdout/...) so a finding is never dropped
  as empty. `_promote_degraded_prose`: when a worker analyzes in PROSE (no
  submit_findings), the agentic adapter parks final_text in a VERIFICATION artifact's
  stdout and marks it degraded; the digest hides verification as plumbing. If there
  are NO signal artifacts but a verification carries substantial prose (>=40 chars),
  promote a copy to a 'finding'. Verified live (a swarm that returned empty now
  returns findings). This normalization lives in Marionette's bridge on purpose (see
  distribution model) -- do NOT move it upstream.
- The `/api/swarm/live` tracker only shows `run_swarm` jobs, not `run_parallel`
  implement workers (those produce worktree patches). An empty tracker for implement
  dispatches is expected, not a bug. Note: run_parallel/run_implement workers often
  report "no changes produced" as a summary even when the patch DID apply -- verify
  the files, don't trust that string.

## Composer / UX
- Steer vs queue while a turn runs: Enter = steer (redirect current turn),
  Cmd/Ctrl+Enter = queue (runs after). Busy toolbar shows Stop + Steer + Queue.
- Prompt queue renders stacked ABOVE the composer (Cursor-style) with drag-reorder,
  edit, remove, "next" badge. Auto-drains: on turn completion the backend runs the
  next queued prompt; from idle, the frontend `maybeDrainQueue` (called from the
  stream's terminal onDone, mirroring maybeRunQueuedResume) fires the next one.
- Queued prompts carry image attachments (transcribed into content on drain); the
  `queued_prompt` event renders the user bubble so an auto-run queued prompt shows in
  chat. Steers carry images via transcription (can't carry raw blocks mid-turn).
- Setup wizard: only auto-opens for genuine first-run (no provider key). Previously
  `seen === null` forced it open on EVERY launch. Now if a key exists it marks
  `pmharness.wizardSeen` and never nags; dismissing it also persists seen.

## Electron loader
- `resolveDistIndex()` (webapp/electron/main.cjs) prefers the checkout's freshly-built
  `webapp/dist` WHENEVER it exists and is non-empty -- dropped the mtime comparison,
  which an asar repack (stamping bundled index.html with "now") could win, pinning the
  UI to a stale bundled dist. This was the "rebuilt UI never appears across relaunches"
  bug. Existence of the checkout build == the user ran `npm run build` == source of truth.

## Deferred on purpose (honest debt)
- `harness/server.py` is ~3958 lines / 111 endpoints. A maintainability nag, not a
  user/reliability risk. Splitting 111 endpoints out of a green, tested module is
  high-churn, high-regression, zero user benefit. Defer until it causes real merge
  pain; then split along the read-only GET seam. Do NOT refactor for aesthetics.
- Wiki orchestrator (portable-llm-wiki, separate repo): a job whose worker process
  dies stays status='running' forever instead of being reaped to interrupted on
  restart (same ghost-spinner class Marionette's own tracker fixed). Cosmetic; does
  not block new ingests.
