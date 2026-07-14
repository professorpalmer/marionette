# Marionette Architecture

A PM-native harness: a coding/agent front-end whose kernel is Puppetmaster
orchestration, driven by swappable open-weights models, with a durable-state GUI.

This document is the single authoritative reference for how the system works and
why it is built this way. It consolidates the validated research (Stages 1-4)
and the product (`harness/`).

## 1. The thesis

Every mainstream agent harness (Cursor, Claude Code, Hermes) treats orchestration
as a tool the model calls, and runs a frontier model as the top-level narrator.
That narrator pays tokens to *talk about* what it will do before doing it.

Marionette inverts this: **Puppetmaster orchestration is the kernel**, and a
cheap open-weights model is a swappable *driver* that emits structured intents,
not prose. The harness executes those intents in code. Two consequences:

- **No frontier narrator tax.** The model spends tokens only on decisions, not
  on narrating tool calls.
- **No vendor black box (optionally).** With self-hosted open weights the entire
  stack is inspectable; with a hosted open-weights provider it is merely cheap.
  These are different deployments of one architecture, not one claim.

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
change was required.

## 3. The driver contract

The driver's entire job is to emit one `DriverIntent` per turn:

```
{ "action": "run_swarm" | "answer" | "stop",
  "goal": "<required for run_swarm>",
  "roles": ["explore", ...]?,          // optional subset
  "worker_mode": "subprocess"?,        // optional
  "rationale": "<one line>" }
```

- `run_swarm` -> the harness executes a Puppetmaster swarm and feeds the real
  artifacts back as the next turn.
- `answer` -> respond directly; no orchestration (the token-thesis decision:
  do not swarm trivia).
- `stop` -> the objective is met; terminate.

`pmharness/intent.py` is the pure contract: a strict validator plus a lenient
text->JSON parser (so "valid JSON" and "valid schema" stay separate metrics).

## 4. The product loop (`harness/conversation.py`)

```
prompt (+ optional images)
   │
   ├─ vision sidecar (if images): VLM transcribes image -> text, prepended
   │
   ▼
 driver.complete(context)  ──(invalid?)──> repair: re-prompt once, then fail clean
   │
   ▼  DriverIntent
   ├─ answer/stop -> emit final, terminate
   └─ run_swarm  -> Orchestrator.run() executes -> REAL artifacts fed back
                    (budget-bounded; over budget -> forced stop)
   ▼ (loop)
```

The pilot loop now lives in `harness/conversation.py` (`ConversationalSession`),
the multi-turn front-end that the GUI and CLI both drive; the older
`harness/session.py` remains the single-shot Session core it grew out of. Every
step is yielded as a `SessionEvent`
(`intent|executing|artifacts|final|error|vision`) so a GUI or CLI renders the
loop live. The driver is config (`HarnessConfig.driver`), swappable in one line.

### Intent repair (`harness/repair.py`)
Verbose reasoning models (e.g. Kimi) wrap JSON in prose. On an unparseable
intent the harness re-prompts ONCE with a strict correction; token accounting
accumulates so repair cost is visible. One retry is the ceiling -- a model that
still can't comply is genuinely unfit, surfaced honestly rather than looped.

## 5. Vision (sidecar, decoupled)

The research found the only vision-capable open *driver* (Kimi) is also the
weakest driver. So the harness does NOT require driver vision. A cheap VLM
sidecar (`harness/vision.py`) transcribes an attached image to TEXT once; the
text is prepended to the driver context. Any text-only driver (glm-5.2,
deepseek, qwen) then "sees" the image through the transcription. The image is
processed once, never re-sent. Vision is a harness capability, like CodeGraph
context injection -- the driver only ever reasons over text.

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

## 8. The evaluation ladder (how the driver choice was justified)

The research is a separate package (`pmharness/`) that validated the driver
layer empirically. Each stage answered one question and exposed the next:

| Stage | Question | Result |
|-------|----------|--------|
| 1 | Does a clean in-process seam exist? | Yes -- driven live, no MCP/CLI. |
| 2 | Can cheap open weights emit valid intents single-turn? | 7 open models tied the Claude control at 100%; qwen at ~1/100th cost. Battery SATURATED (does not rank). |
| 3 | Can they drive a multi-turn loop? | Discriminating, but the spread was a HARNESS confound (thin artifacts starved careful models). |
| 3.5 | (de-confound) budget + substantive substrate | Claude 55%->100%; whole open field 100%, indistinguishable from frontier. Lesson: harness design is a lever on driver economy. |
| 4 | Rank via read-decide traps (inconclusive vs conclusive) | Discriminates competent-vs-lazy (offline lazy stub fails); frontier models genuinely read findings. Splitting the top tier needs the open-weights field on Stage 4 (pending key). |

**Default driver: qwen3-coder-30b** -- Apache-2.0, the lowest-token winner of
both eval batteries at 100% quality (the value `harness/config.py` actually
sets). Swappable to any catalog model via config.

Results live in `results/STAGE*_RESULTS.md` (Stages 1-3.5) and
`results/STAGE4_FIELD_RESULTS.md` (the ranked open-weights field). Every score is
from real execution
against real Puppetmaster; an early false "30%" run (a masker-truncated API key)
was caught by the eval's own token instrumentation and never reported.

## 9. Package map

```
pmharness/        research rig (validates the driver layer)
  intent.py         DriverIntent contract + validator + parser
  bridge.py         intent -> Orchestrator.run -> normalized result
  drivers/          Driver protocol; OpenAICompat (Kimi/GLM/...), Anthropic,
                    BedrockDriver (Converse/ConverseStream), stubs
  registry.py       data-driven catalog.json (license, price, vision, tier)
  battery* / scoring* / runner* / episode*   the Stage 1-4 evals
  ledger.py         append-only SQLite results
harness/          the product (~43 modules; principal ones below)
  conversation.py   the pilot loop (ConversationalSession, multi-turn front-end)
  session.py        single-shot Session core (SessionEvent) the loop grew from
  server.py         stdlib HTTP + SSE server (three-pane GUI backend)
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
  repair.py         intent-repair retry
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
