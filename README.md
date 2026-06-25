# pm-harness

Research rig for the question: **which LLMs can drive Puppetmaster as a native
harness layer?**

This is Stage 2 of the PM-native harness investigation. Stage 1 proved that a
native driver can call Puppetmaster's orchestration engine in-process (no MCP,
no CLI subprocess). Stage 2 measures **which models can actually do the
driving** -- emit valid structured orchestration intents and make the right
call about when to orchestrate vs. answer vs. stop.

Internal-first. stdlib-only rig (urllib + sqlite); Puppetmaster is the one real
dependency, installed editable from the local checkout.

## The thesis it tests

A PM-native harness deletes the frontier-model-as-narrator layer. Instead of a
model narrating "I'll now call puppetmaster_start_swarm" in prose (paying tokens
to talk), the driver model emits one compact JSON **DriverIntent** and the
harness executes it in code. The open question is empirical: can cheap
open-weights models (Kimi, GLM) hit that structured target reliably enough to
run the loop, and how close do they get to a frontier control?

## The seam (Stage 1, proven)

```
MCP tools   ─┐
             ├─→  CLI `run`  ─→  Orchestrator(store).run(...)   ← the real engine
CLI commands ┘                        ↑
                       pm-harness calls THIS directly
```

`Orchestrator(store).run(goal, roles=, specs=, worker_mode=, on_job_created=)
-> RunResult(job, artifacts, summary, summary_path, rerouted_tasks, mode)`.
`run()` blocks; live observation uses the store's event layer
(`read_events_since` / `event_cursor` / `wait_for_events`).

## Architecture

| Module | Role |
|--------|------|
| `pmharness/intent.py` | `DriverIntent` contract + strict validator + lenient text->JSON parser. The pure layer; no PM dependency. |
| `pmharness/bridge.py` | Executes a validated `run_swarm` intent against Puppetmaster's in-process Orchestrator (local adapter; deterministic, free). |
| `pmharness/drivers/` | `Driver` protocol. `StubDriver` (offline oracle / ceiling). `OpenAICompatDriver` (Kimi, GLM, OpenAI -- one driver, every OpenAI-compatible endpoint). |
| `pmharness/battery.py` | 10 labeled tasks across three buckets: swarm / answer / stop. |
| `pmharness/scoring.py` | Deterministic, no LLM-as-judge: json_valid, schema_valid, action_correct, executed_ok, composite score. |
| `pmharness/ledger.py` | Append-only SQLite record of every attempt. |
| `pmharness/runner.py` + `scripts/run_eval.py` | Run the battery per model, persist, print the table. |

## Design decision: driver eval vs. worker eval

The eval grades **driving**, not **working**. Swarm intents execute on
Puppetmaster's free **local adapter** so ground truth is deterministic and
key-free. "Is Kimi a good *worker*?" is a separate study (it would route Kimi
into the swarm itself). Conflating the two would poison the cost thesis, so the
rig keeps them apart by construction.

## Metrics

- **json_valid** -- driver output parsed to a JSON object at all
- **schema_valid** -- parsed object is a real `DriverIntent`
- **action_correct** -- decision matched the task's ground-truth label
- **executed_ok** -- for swarm cases, Puppetmaster returned >= 1 artifact
- **score** -- composite 0..1 (schema floor 0.40, decision 0.40, execution 0.20)
- plus tokens_out and latency per model (the cost-thesis columns)

## Model registry (the research artifact)

`pmharness/catalog.json` is the data-driven list of harness-driver candidates,
verified on Hugging Face + provider pricing (2026-06-24). Each entry carries
license and native $/Mtok so the eval reports cost alongside driver score.

| Tier | Models | Why |
|------|--------|-----|
| **flagship** | glm-5.2 (MIT), kimi-k2.6, minimax-m2.7, deepseek-v4-pro (MIT) | Can drive the top-level loop |
| **value** | deepseek-v4-flash (MIT), glm-4.7-flash (MIT), qwen3-coder-30b (Apache-2.0), minimax-m2.5-highspeed | Cheap fodder the PM router sends bulk sub-tasks to |
| **frontier_control** | gpt, claude | The ceiling the open-weights rows are judged against |

License note: GLM / DeepSeek / Qwen are clean MIT/Apache; Kimi / MiniMax ship
under `license:other` (provider's own license). Irrelevant to driver scoring,
but load-bearing for the enterprise self-host pitch.

## Reach: one key vs. native

- **`--reach openrouter`** (default): the entire field through one
  OpenAI-compatible endpoint with one key (`OPENROUTER_API_KEY`). Best for
  breadth -- study everyone fast.
- **`--reach native`**: each provider's own endpoint + key
  (`ZAI_API_KEY`, `MOONSHOT_API_KEY`, `MINIMAX_API_KEY`, `DEEPSEEK_API_KEY`,
  `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`). Use for finalists where the cost
  receipt must reflect true native pricing, not OpenRouter markup.

Driver-quality measurement is identical regardless of reach.

## Run it

```bash
python -m venv .venv && .venv/bin/pip install -e /path/to/Puppetmaster pytest
.venv/bin/python -m pytest -q                                 # 29 tests, fully offline
.venv/bin/python scripts/run_eval.py --drivers stub-oracle    # offline, no keys

# Whole open-weights field through OpenRouter (one key):
export OPENROUTER_API_KEY=***
.venv/bin/python scripts/run_eval.py --drivers all --reach openrouter

# Flagship tier on native endpoints (cost-accurate):
.venv/bin/python scripts/run_eval.py --tier flagship --reach native
```

## Status

- Stage 1 (seam) proven; Stage 2 rig built and green end-to-end offline (29 tests).
- `stub-oracle` scores 100% (control ceiling) driving real Puppetmaster.
- Registry covers 10 current models across 3 tiers; open-weights + frontier rows
  pending API keys.


## The Harness (product scaffold)

Beyond the research rig, `harness/` is the productization: a PM-native harness
with a Cursor 3.0 / Hermes-style three-pane GUI.

- `harness/session.py` -- the driver loop: prompt -> open-weights driver emits a
  DriverIntent -> real Puppetmaster Orchestrator executes -> REAL artifacts fed
  back -> budget-bounded -> terminates. Yields structured events for the GUI.
- `harness/state.py` -- DurableState: clean read layer over SwarmStore (jobs,
  artifacts, live event stream). What the right pane renders.
- `harness/config.py` -- swappable driver (default glm-5.2: MIT, efficient,
  100% on the discriminating eval), reach, budget.
- `harness/server.py` + `harness/web/` -- stdlib HTTP server streaming Session
  events over SSE to a three-pane dark UI (left nav + driver card + jobs,
  center driver-loop conversation, right durable-state artifacts).

```bash
# live (glm-5.2 driver):
export OPENROUTER_API_KEY=***
./scripts/harness_gui.sh                 # http://127.0.0.1:8799

# no-key demo (stub driver, still drives real Puppetmaster):
HARNESS_DRIVER=stub-oracle-v2 ./scripts/harness_gui.sh
```

The driver is config -- the research proved the whole open-weights field drives
at 100% under this harness, so swapping glm-5.2 for kimi/deepseek/qwen is a
one-line change.


## Vision (sidecar -- decoupled from the driver)

The research found the only vision-capable open DRIVER (Kimi) is also the
weakest driver. So the harness does NOT require a vision-capable driver. Instead
`harness/vision.py` runs a cheap VLM sidecar that transcribes an attached image
to TEXT once; that text is prepended to the driver context, so any text-only
driver (glm-5.2, deepseek, qwen) "sees" the image through the transcription. The
driver never receives pixels; the image is processed once.

```python
session.run("What is in this screenshot?", images=["/path/to/shot.png"])
# -> emits a "vision" event with the transcription, then drives normally
```

Default sidecar is Gemini's vision endpoint (stand-in); swap base_url/model/key
for an open VLM (GLM-OCR / Kimi-VL / Qwen-VL) -- same image->text contract.
Verified live: a screenshot of `AUTH_TOKEN=*** was transcribed and a text-only
driver correctly answered "abc123" from the text alone.


## Headless CLI

The harness runs without a browser:

```bash
harness "Investigate auth across this repo and conclude"
harness --driver glm-5.2 --budget 4 "Audit for the biggest risk"
harness --image shot.png "What secret is in this screenshot?"   # vision sidecar
harness --json "..."   # machine-readable event stream
```

Exit codes: 0 = terminated cleanly (answer/stop), 1 = error, 2 = forced stop
(budget/turn cap). Streams the driver loop to the terminal (intent / executing /
artifacts / final), colored on a TTY.

## GUI: uploads, errors, history

The three-pane GUI (`./scripts/harness_gui.sh`) now supports:
- Image attach (button + drag/drop onto the center pane) -> multipart upload ->
  the vision sidecar transcribes -> the driver reasons over text. Attachment
  chips show pending images.
- Distinct error cards for driver / transport / execute / invalid-intent
  failures (with the raw model output on a parse error) -- nothing fails silent.
- Vision events render inline (transcription char count + preview).
- Job history persists in Puppetmaster's store and reloads on page load; click a
  job to inspect its artifacts in the right pane.


## Reproducible eval + config

```bash
harness eval                          # offline oracle smoke (no keys)
harness eval --driver glm-5.2 --reach openrouter --stage s4   # one live driver
harness --version
```

Config layering (env wins over file wins over defaults): set defaults in
`~/.harness.json` (`{"driver": "glm-5.2", "budget": 3}`) and override per-run
with `HARNESS_DRIVER=...`. Missing-key is handled gracefully -- the CLI and GUI
surface a clear, actionable message (which env var to set, or use the stub
driver) instead of a stack trace.


## Health check

```bash
harness doctor          # verify seam, store, driver key, vision key -- one shot
harness doctor --ping   # also make a live 1-token call to the driver
```

Reports ok / warn / fail per check with an actionable hint. The Puppetmaster
seam and durable store are hard checks (fail -> exit 1); a missing driver or
vision key is a warning (the harness still loads, stub drivers still work). Run
this first on a new machine to see exactly what to set.


## Fully-open vision (no frontier model anywhere)

By default the vision sidecar uses Gemini (a stand-in). Set
`HARNESS_VLM_REACH=openrouter` to route an OPEN VLM instead -- default
`qwen/qwen3-vl-30b-a3b-instruct` (Apache-2.0, pairs with the qwen3-coder
driver), overridable via `HARNESS_VLM_MODEL`. With this, the whole path is open:
open VLM transcribes -> open driver reasons -> Puppetmaster orchestrates. No
black box anywhere. Verified live: qwen3-vl transcribed a screenshot (AUTH_TOKEN
+ a function), the qwen3-coder driver answered correctly from the text alone.

## Dogfooding notes

Driven live on qwen3-coder-30b against real tasks: trivia -> answer (0 swarms),
underspecified prompts -> answer (no wasted swarm), open-ended audits -> multi-
swarm investigation then a grounded conclusion. qwen is thorough on audit-style
prompts (it investigates multiple angles up to budget); lower `--budget` for a
snappier loop, raise it for deeper investigation. The GUI thinking-indicators
keep long multi-swarm runs legible.


## Real codebase analysis (read-only)

By default swarms run on a deterministic local substrate (free, no real code
analysis -- labeled "demo substrate" in output). To analyze a REAL repo:

```
harness --repo /path/to/repo --swarm-adapter openai \
        "Investigate how X works here and report findings."
```

This routes read-only analysis workers (CodeGraph-injected) against the repo via
OpenRouter (open model, default qwen3-coder-30b; override HARNESS_ANALYSIS_MODEL).
Safety: triple-guarded read-only -- the openai adapter is not edit-capable, specs
carry read_only/no_edit/dry_run, and a test asserts the target repo is never
mutated. Safe even on live-service repos.

Honest limit: a cheap 30B analysis model reads the right files (real file-cited
evidence) but can fabricate specifics -- findings are directionally useful and
need verification. Pin a stronger HARNESS_ANALYSIS_MODEL for high-stakes work.


## The pilot (conversational UX)

The harness is a conversation, not a raw control loop. You message a PILOT (the
model you pick), it plans in prose, investigates by firing swarms, and explains
the findings -- like Cursor/Hermes. Orchestration calls render as Cursor-style
COLLAPSIBLE cards (collapsed header "Ran swarm: <goal>"; expand for goal, cwd,
artifacts), so you see the controls without them being the whole output.

- The pilot is SWAPPABLE in the UI (qwen / glm / deepseek / kimi / ... per your
  keys) -- pick how much to spend on the conversation layer.
- Transcript lives in the you<->pilot channel only. Swarm workers receive a
  DISTILLED brief (the pilot's goal) + CodeGraph -- never the transcript. The
  expensive investigation stays decoupled from the conversation.
- Set --repo + --swarm-adapter openai for the pilot's swarms to do REAL read-only
  codebase analysis (else demo substrate; a stall guard stops the pilot grinding
  on demo artifacts and tells you to enable real analysis).

Endpoints: GET /api/chat?message=... (SSE: message | action_start | action_result
| assistant_done), GET /api/pilot?model=... (hot-swap the pilot).
