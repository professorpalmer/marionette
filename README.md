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
