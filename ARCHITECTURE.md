# Marionette Architecture

A PM-native coding harness: Marionette is a swappable pilot shell (frontier or
open-weights) whose kernel is Puppetmaster orchestration and durable state, with
a three-pane GUI over structured tool calls.

This document is the single authoritative reference for how the system works and
why it is built this way. It consolidates the validated research (Stages 1-4)
and the product (`harness/`).

## 1. The thesis

Every mainstream agent harness (Cursor, Claude Code, Hermes) treats orchestration
as a tool the model calls from inside a frontier narrator that also owns the
conversation. Marionette keeps the structured-tool idea and makes the split
explicit:

- **Marionette is the pilot shell.** The user talks to a swappable model
  (frontier or cheap open-weights). That pilot emits a `PilotTurn`: prose for
  the human plus zero-or-more structured actions (native `tool_calls` or the
  JSON envelope). The shell does not narrate orchestration in free prose; it
  calls tools the harness executes in code.
- **Puppetmaster is the kernel underneath.** Durable `SwarmStore` state,
  `Orchestrator.run`, swarm/implement workers, and CodeGraph live in
  Puppetmaster. Delegation verbs (`run_swarm`, `run_implement`, `run_parallel`,
  `route_task`, …) are tools the pilot calls; they are not a separate product
  loop.

Two consequences of that lane-B framing:

- **Any model can drive.** Hot-swap the pilot by task. A cheap open-weights
  model handles inline work; heavier reasoning and multi-file changes go to
  Puppetmaster workers the router selects. Open-weights-as-sole-driver was the
  research heritage that proved the seam; it is not the shipping product claim.
- **No vendor black box (optionally).** With self-hosted open weights the
  stack is inspectable; with a hosted provider it is merely swappable and
  accountable. These are different deployments of one architecture.

Ownership rule for contributors: new pilot tools land in `pilot.py` schema plus
`tool_dispatch` / `tool_discovery`; new orchestration lands in Puppetmaster;
`conversation.py` composes the turn loop (history, SSE, swarm bridge) from
mixins — prefer peeling helpers into a focused mixin/`harness/api/*` module over
growing the facade.

## 2. The seam (Stage 1, proven live)

Puppetmaster's MCP tools and CLI are both thin transports over one engine:

```
MCP tools   ─┐
             ├─→  CLI `run`  ─→  Orchestrator(store).run(...)   ← the engine
CLI commands ─┘
```

A native harness deletes both transports and calls the engine in-process:

```python
from puppetmaster.store_factory import create_store
from puppetmaster.orchestrator import Orchestrator

store  = create_store("sqlite", state_dir)
result = Orchestrator(store).run(goal, roles=, worker_mode=, on_job_created=)
# RunResult(job, artifacts, summary, summary_path, recovered_tasks,
#           rerouted_tasks, mode)
```

`run()` blocks; live observation uses the store event layer
(`read_events_since` / `event_cursor` / `wait_for_events`). No Puppetmaster core
change was required. Stage 1 proof lives in `FINDINGS.md` (no separate
`results/STAGE1_RESULTS.md` file).

## 3. The product pilot contract

The shipping product contract is a conversational turn, not a single bare
intent. `harness/pilot.py` defines `PilotTurn` / `PilotAction` and the tool
schema the providers see:

```
{ "say": "<prose for the user>",
  "actions": [
    { "kind": "read_file" | "run_command" | "run_swarm" | ...,
      ...kind-specific fields }
  ] }
```

- `say` is transcript prose shown to the human.
- `actions` is zero or more structured calls. Empty actions => the pilot is
  done talking for this turn and yields to the user.
- When actions are present, the harness executes them (via
  `ToolDispatchMixin`), feeds results back, and the pilot continues until a
  turn with no actions.

Provider-native pilots emit the same actions as native `tool_calls`; the
envelope above is the parseable fallback. `run_swarm` is one tool among many,
not the whole product contract.

### Tool catalog (core vs discovery)

`harness/tool_discovery.py` keeps a small always-visible core in the pilot
prompt and lets the agent activate peripherals with `search_tools`. Core stays
filesystem/edit/shell, CodeGraph/wiki/memory, MCP management, and PM
delegation verbs so swarm gates cannot deadlock. Power-user tools (browser_*,
lsp, web_*, read_pdf, search_state, view_image, mcp_* schemas) stay hidden
until activated. Handlers live in `ToolDispatchMixin`
(`harness/tool_dispatch.py`), not as new methods on the turn loop.

## 4. The product loop (`harness/conversation.py`)

```
prompt (+ optional images)
   │
   ├─ vision sidecar (if images): VLM transcribes image -> text, prepended
   │
   ▼
 pilot.complete / native tool_calls  ->  PilotTurn (say + actions)
   │
   ├─ no actions -> emit message, yield to user
   └─ actions -> ToolDispatchMixin._do_* handlers
                    │
                    ├─ local tools (read/edit/shell/…)
                    └─ PM verbs (run_swarm / run_implement / …)
                         -> Orchestrator / store events
                         -> REAL artifacts fed back into the transcript
   ▼ (loop until empty actions)
```

`ConversationalSession` is the multi-turn front-end the GUI and CLI drive; the
older `harness/session.py` remains the single-shot Session core used by the
research rig. Every step is yielded as a session event
(`message|action_start|action_result|assistant_done|error|…`) so a GUI or CLI
renders the loop live. The pilot model is config (`HarnessConfig.driver`),
swappable in one line.

## 5. Vision (sidecar, decoupled)

The research found the only vision-capable open *driver* (Kimi) is also the
weakest driver. So the harness does NOT require pilot vision. A cheap VLM
sidecar (`harness/vision.py`) transcribes an attached image to TEXT once; the
text is prepended to the pilot context. Any text-only pilot (glm-5.2,
deepseek, qwen, frontier) then "sees" the image through the transcription. The
image is processed once, never re-sent. Vision is a harness capability, like
CodeGraph context injection -- the pilot only ever reasons over text.

The sidecar is resolved in tiers (`harness/vision.py` `default_sidecar`): an
explicit `HARNESS_VLM_REACH` override (e.g. `openrouter` for an open VLM) ->
a dedicated Gemini/OpenRouter vision key -> ANY already-configured provider that
exposes a vision model (Anthropic/OpenAI/xAI via `provider_vision_sidecar`) ->
`NullVisionSidecar`, which raises an actionable error rather than crashing. So no
specific vision vendor is required: if you have configured any vision-capable
provider, images work out of the box.

## 6. Durable state (`harness/state.py`)

A read-only layer over Puppetmaster's `SwarmStore`: `list_jobs`,
`job_artifacts`, and the live event stream. This is what the GUI's right pane
renders. The Session writes (by driving the Orchestrator); DurableState reads.
Because state lives in Puppetmaster's store, job history persists across harness
restarts for free. Alongside it, the harness persists sessions, transcripts, and
the prompt queue under a stable `HARNESS_STATE_DIR` (default `~/.pmharness/state`):
`prompt_queue.json` plus `transcripts/{id}.json`, so an in-flight playlist of
prompts and prior conversation survive a backend restart.

## 7. Surfaces

- **GUI** (`webapp/` + `harness/server.py`): the shipping UI is the Electron
  app in `webapp/` (React renderer, IPC bridges, three-pane layout). The stdlib
  HTTP + SSE backend streams session events to the renderer. `harness/web/` is a
  legacy browser fallback only (`docs/LEGACY_FRONTEND.md`); new UI work belongs
  in `webapp/src`. Mid-turn SSE reattach: if the renderer drops the live stream,
  `GET /api/chat/events?since=<cursor>` replays from a bounded per-generation
  ring; `ring_miss`, `generation_mismatch`, or `cursor_gap` responses trigger a
  hydrate path instead of silently skipping events.
- **CLI** (`harness/cli.py`): one entrypoint with subcommands --
  `harness "<task>"` (run), `harness gui` (UI), `harness eval` (the Stage 1-4
  ladder), `harness doctor` (health check), `harness --version`. Run flags:
  --driver/--budget/--image/--json; exit codes 0 clean / 1 error / 2 forced.
  Same Session core; graceful missing-key handling (preflight) on every path.

### 7a. HTTP surface security

The GUI server (`harness/server.py`) is local-first but not trust-open. A
per-process auth token (`_TOKEN`, `HARNESS_TOKEN` override) is written chmod-600
and checked by a CENTRALIZED gate in `do_GET`: every path is rejected unless the
request carries the token, with a single public allowlist -- `_PUBLIC_GET_PATHS =
{"/", "/index.html", "/app.js", "/app.css"}` (the static shell that bootstraps
the authenticated client). Host and Origin are also validated. The API is
authenticated by default; there is no per-endpoint opt-in.

### 7b. Token economics (prompt caching + real context meter)

The cost thesis is enforced with real accounting, not estimates:

- **Multi-provider prompt caching.** The Anthropic driver
  (`pmharness/drivers/anthropic.py`) sets a stable plus a moving cache breakpoint
  so the large, unchanging prefix is served from cache across turns.
- **Cache-read billing.** `harness/server.py` prices cached prompt tokens at a
  steep discount (`CACHE_READ_MULTIPLIER = 0.1`) inside `_session_cost` /
  `_job_cost`, and surfaces the dollars saved via `_cache_savings` ->
  `cache_savings_usd` in job/session payloads.
- **Two spend surfaces.** The status bar aggregates **process-wide** pilot +
  delegated-job spend for the running backend. The Swarm pane shows **per-repo
  session** spend on each job card (`/api/swarm/live?repo=...`); it is scoped to
  the active workspace, not a duplicate footer total.
- **Real-usage context meter.** `harness/conversation.py` tracks
  `_last_prompt_tokens` from actual provider usage, so the context gauge reflects
  measured prompt size rather than a heuristic.

## 8. Research rig (`pmharness/`) and the evaluation ladder

The research package (`pmharness/`) validated the driver layer empirically. It is
not the shipping GUI contract. Its unit of work is a bare `DriverIntent`
(`run_swarm` | `answer` | `stop`) from `pmharness/intent.py`, executed by
`pmharness/bridge.py` against in-process Puppetmaster. `harness/session.py` plus
`harness/repair.py` are the single-shot / intent-repair path used by that rig;
the product path is `ConversationalSession` + `PilotTurn` (sections 3-4).

```
{ "action": "run_swarm" | "answer" | "stop",
  "goal": "<required for run_swarm>",
  "roles": ["explore", ...]?,
  "worker_mode": "subprocess"?,
  "rationale": "<one line>" }
```

Each stage answered one question and exposed the next:

| Stage | Question | Result |
|-------|----------|--------|
| 1 | Does a clean in-process seam exist? | Yes -- driven live, no MCP/CLI. Documented in `FINDINGS.md` (no `STAGE1_RESULTS.md`). |
| 2 | Can cheap open weights emit valid intents single-turn? | 7 open models tied the Claude control at 100%; qwen at ~1/100th cost. Battery SATURATED (does not rank). |
| 3 | Can they drive a multi-turn loop? | Discriminating, but the spread was a HARNESS confound (thin artifacts starved careful models). |
| 3.5 | (de-confound) budget + substantive substrate | Claude 55%->100%; whole open field 100%, indistinguishable from frontier. Lesson: harness design is a lever on driver economy. |
| 4 | Rank via read-decide traps (inconclusive vs conclusive) | Discriminates competent-vs-lazy (offline lazy stub fails); frontier models genuinely read findings. Splitting the top tier needs the open-weights field on Stage 4 (pending key). |

**Default driver: qwen3-coder-30b** -- Apache-2.0, the lowest-token winner of
both eval batteries at 100% quality (the value `harness/config.py` actually
sets). Swappable to any catalog model via config.

Score tables live in `results/STAGE2_RESULTS.md`, `results/STAGE3_RESULTS.md`,
`results/STAGE3_5_RESULTS.md`, `results/STAGE3_5_FIELD_RESULTS.md`, and
`results/STAGE4_FIELD_RESULTS.md`. Stage 1 has no results file; the live seam
proof is in `FINDINGS.md`. Every score is from real execution against real
Puppetmaster; an early false "30%" run (a masker-truncated API key) was caught by
the eval's own token instrumentation and never reported.

## 9. Package map

```
pmharness/        research rig (validates the driver layer; not the GUI contract)
  intent.py         DriverIntent contract + validator + parser
  bridge.py         intent -> Orchestrator.run -> normalized result
  drivers/          Driver protocol; OpenAICompat (Kimi/GLM/...), Anthropic,
                    BedrockDriver (Converse/ConverseStream), stubs
  registry.py       data-driven catalog.json (license, price, vision, tier)
  battery* / scoring* / runner* / episode*   the Stage 1-4 evals
  ledger.py         append-only SQLite results
harness/          the product (principal modules below; mixins compose the session)
  conversation.py   ConversationalSession facade (composes mixins; thin turn owner)
  send_loop.py      SendLoopMixin — send / _send_locked_inner turn kernel
  busy_control.py   BusyControlMixin — busy / interrupt / reap
  prompt_queue.py   PromptQueueMixin
  adapter_resolve.py AdapterResolveMixin (implement adapter pick)
  steer_mixin.py / compaction_mixin.py / local_jobs.py
  wiki_distill.py / review_memory.py
  turn_economy.py   TurnEconomy facade over budget/savings helpers
  pilot.py          PilotTurn / PilotAction contract + tool schema + PILOT_SYSTEM
  tool_discovery.py ToolCatalog (core-visible vs search_tools activation)
  tool_dispatch.py  ToolDispatchMixin (per-tool `_do_*` handlers)
  pilot_guards.py   pilot-loop safety / policy guards
  hash_edit.py      hashline surgical edit ops (optional via HARNESS_HASH_EDIT)
  api/              HTTP route peels (sessions, jobs, sse, streams, wiki, mcp,
                    providers, files, attach, skills, auth, worktrees,
                    terminals, commands, hooks, checkpoints, git, reviews,
                    registry, platform, codegraph, workspace, settings,
                    session_control, usage, cost, pilot, static) wired from
                    server.Handler. Handler keeps auth/token gates; route
                    bodies live in api/* with *Services dataclasses. auth
                    re-exports providers' pool/OAuth/Cursor-CLI handlers
                    under an ownership name. platform also owns Bedrock
                    BYOK; codegraph owns GET status panel plus POST
                    reindex/apply-excludes and (via codegraph_index) the
                    background indexer/status/stale-refresh runtime;
                    workspace owns open/forget/get/symbols/workspaces CRUD
                    plus recent-list persistence helpers; static owns the
                    legacy browser-shell GET /, /index.html, /app.js,
                    /app.css bodies (token meta injection); settings owns
                    /api/settings and /api/config; session_control owns
                    stash/interrupt/rewind/steer/queue plus persist/compact/
                    state/context_at/swarm-results and restart-prepare
                    (process self-terminate stays on Handler); usage owns
                    /api/usage and /api/context/usage; cost owns shared
                    session/swarm cost math, boot-meter carry/persist, usage
                    response cache, and scoped job-store merges consumed by
                    usage/jobs via injected callables (server re-exports
                    historical ``_`` names); sse owns /api/chat/events
                    replay; pilot owns /api/pilot hot-swap; terminals also
                    owns /api/terminal/stream SSE.
  session.py        single-shot Session core (DriverIntent path for the research rig)
  repair.py         intent-repair retry (research / bare-intent path)
  server.py         stdlib HTTP + SSE server shell (routes live in api/)
  worker.py         edit-capable worktree worker (applies real file edits)
  worktrees.py      git worktree confinement (isolated work trees)
  workspaces.py     workspace bookkeeping over the confined worktrees
  command_policy.py full-auto danger guard (blocks unsafe shell commands)
  wiki.py           portable knowledge wiki (durable notes store)
  wiki_orchestrator.py  drives wiki page synthesis from session output
  skill_distiller.py    distills reusable skills from completed runs
  skill_store.py    persists and retrieves learned skills
  mcp_client.py     MCP transport client
  mcp_manager.py    MCP server lifecycle + tool registry
  state.py          DurableState (read layer over SwarmStore)
  config.py         HarnessConfig (swappable driver, budget, reach)
  autobudget.py     full-auto budget governor (caps spend per objective)
  checkpoints.py    run checkpoints for resume/rollback
  memory_store.py   durable user facts and preferences
  providers.py      model provider/reach resolution
  vision.py         VLM sidecar (image -> text)
  cli.py            headless CLI
```

### 9a. Full-auto mode and the AutoBudget governor

Full-auto mode lets the pilot loop run an objective to completion across many
turns without per-step confirmation. `harness/autobudget.py` is the governor
that bounds it: it tracks spend against a per-objective ceiling and forces a
clean stop before the budget is exceeded, so an unattended run cannot spiral.
This keeps the cost thesis intact even when no human is watching the loop.

### 9b. The command safety guard (`command_policy.py`)

Because full-auto can execute shell commands without a human in the loop,
`harness/command_policy.py` is the danger guard that screens commands before
they run. It blocks destructive or unsafe operations rather than trusting the
driver, turning autonomy into a bounded, auditable capability.

### 9c. The portable Wiki orchestrator

`harness/wiki.py` is a portable knowledge wiki -- a durable notes store the
system can carry between sessions. `harness/wiki_orchestrator.py` drives
synthesis of wiki pages from session output, accumulating reusable project
knowledge over time. Paired with the skill store, this is how the harness
learns from its own runs instead of starting cold each time.

## 10. Non-goals (v1, internal-first)

- Not Cursor parity. Not a model-training pipeline. Not public.
- Vision sidecar uses a frontier VLM as a stand-in; swapping an open VLM
  (GLM-OCR / Kimi-VL / Qwen-VL) is a config change, not a redesign.
- The "no black box" enterprise claim requires self-hosted open weights; the
  hosted-provider path is the cheap-not-private deployment. Stated per audience.
