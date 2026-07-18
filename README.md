# Marionette

A desktop AI coding harness where the LLM is a **component inside** the kernel,
not the platform. Marionette drives any model -- frontier or cheap open-weights --
through a structured pilot loop over [Puppetmaster](https://github.com/professorpalmer/Puppetmaster)
durable state, with CodeGraph-aware retrieval, a portable cross-session knowledge
wiki, and multi-worker delegation.

Internal-first research rig and daily-driver app. stdlib-only backend (urllib +
sqlite); Puppetmaster is the one real dependency, installed editable from a local
checkout.

> Status: v0.9.84, deliberately pre-1.0. Vetted privately before any wider release.

## Documentation

Start here, then follow the map:

| Doc | What's in it |
|---|---|
| [README](README.md) (this file) | What Marionette is, capabilities, install, run, configure. |
| [ARCHITECTURE.md](ARCHITECTURE.md) | The three-pane app, the pilot loop, module map, data flow. |
| [DISTILLER_ARCHITECTURE.md](DISTILLER_ARCHITECTURE.md) | How completed sessions distill into durable skills/rules and wiki pages. |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Dev setup, conventions, how self-updating works for contributors. |
| [RELEASING.md](RELEASING.md) | Dual distribution: source installer plus optional thin Electron shell; tagging and GitHub Releases. |
| [AGENTS.md](AGENTS.md) | Rules for agents/contributors working in this repo. |
| [DEMO.md](DEMO.md) | A guided walkthrough of the harness in action. |
| [FINDINGS.md](FINDINGS.md) | Research-rig findings: which models can drive the harness. |
| [NOTICE.md](NOTICE.md) | Third-party attributions. |
| [docs/discord-mcp.md](docs/discord-mcp.md) | Optional recipe: wire a MIT Discord MCP (Docker + `manage_mcp`), not a first-party Discord product. |

## What it is

Marionette is a three-pane desktop app (Electron + a stdlib Python backend over
SSE):

- **Center -- the pilot loop.** A conversational driver: you talk to it, it emits
  structured tool calls (read, search, edit, run, delegate), and the harness
  executes them in code. No "I will now call the tool" narrator tax.
- **Right -- tools on demand.** Default closed with a slim dock (Swarm, Changes,
  Browser, Terminal, State). State holds CodeGraph, Wiki, and MCP. Open restores
  last width on the chosen tab. Optional Firecrawl: set `FIRECRAWL_API_KEY`, then
  add Firecrawl from State â†’ MCP (catalog entry; not baked into native `web_fetch`).
- **Left -- workspace.** Projects, git branches/worktrees, sessions (auto-named
  from the first message), and the Puppetmaster job list.

The thesis: there is no single best model. Hot-swap the driver by task. A cheap
local model (e.g. qwen3-coder-30b via OpenRouter, cents per session) handles
inline work; heavier reasoning and multi-file changes delegate to Puppetmaster
workers the router selects.

### Aligned incentives (why a kernel, not a model vendor)

A first-party coding tool from a model lab sells you tokens -- every optimization
that cuts your token bill cuts their revenue. They build prompt caching because
competitive pressure forces it, then stop exactly where "competitive" ends: cached
reads are billed at ~10% of input, not free, and the savings are rarely shown to
you at all. Marionette makes nothing from your token count, so its incentives point
one way -- minimize your spend and prove it:

- **Send less.** CodeGraph-first retrieval injects targeted symbols instead of
  dumping whole files, so prompts start small.
- **Re-send cheap.** Multi-provider prompt caching (stable + moving breakpoints)
  serves the large, unchanging prefix from cache every turn.
- **Bill honestly.** Cached tokens are priced at the real cache-read discount and
  the dollars saved are shown live in the status bar -- not hidden.
- **Route to the cheapest sufficient model**, including a competitor's open-weights
  model. A model vendor will never route you off its own tokens; an independent
  kernel will. That is an alignment a first-party tool structurally cannot match.

### Benchmarks and evidence

The cost thesis is measured, not asserted:

- **SWE-bench Lite (cost/quality).** A controlled 3-arm study holding the model
  ceiling constant and varying only the orchestration machinery: the
  CodeGraph-context + router configuration is about **47 percent cheaper** than
  the frontier baseline at equal quality. Frozen predictions re-grade
  deterministically with just Docker (no API keys):
  [swebench-pm](https://github.com/professorpalmer/swebench-pm).
- **NL2Repo-Bench (build a library from a spec).** Durable-state orchestration
  reaches a **91.1 percent mean test-pass rate, about 2.28x the ~40 percent
  published state of the art**, and solves 53 percent of libraries to a fully
  green upstream suite. Full method and caveats in the paper:
  [durable-state-vs-context](https://professorpalmer.github.io/durable-state-vs-context/)
  ([DOI](https://doi.org/10.5281/zenodo.20709565)).

## Core capabilities

| Capability | What it does |
|---|---|
| **Provider-native pilot** | One driver, every OpenAI-compatible endpoint (OpenRouter or native). Frontier control models (Claude, GPT) and open-weights (GLM, DeepSeek, Kimi, Qwen, MiniMax) drive the same loop. |
| **CodeGraph-first retrieval** | Per-turn structural context is auto-injected (symbols, defs, call sites) before the model acts, so it leans on the graph instead of dumping whole files. Self-healing: the index detects edits, additions, and deletions and refreshes in the background. |
| **Puppetmaster delegation** | run_swarm (read-only analysis), run_implement (edit-capable worktree worker), run_parallel (concurrent waves). Heavy/multi-file work runs as durable, auditable jobs. Requires `puppetmaster-ai==1.20.0` (includes `prewalk` plan-then-cheap). |
| **Portable LLM Wiki** | Cross-session, cross-LLM durable memory. A local model structures a session digest into entity/concept/decision pages (the "backwards" orchestration) cheaply, then ingests them -- human-approved by default. |
| **Vision on any driver** | Paste or drop a screenshot and even a text-only driver "sees" it. A VLM sidecar transcribes the image, resolved in tiers: an explicit `HARNESS_VLM_REACH` override, then a dedicated Gemini/OpenRouter vision key, then -- with zero extra setup -- **any provider key you already have that exposes a vision model** (Anthropic, OpenAI, xAI, ...). No separate vision key required if your driver's provider can see. |
| **Honest token economics** | Prompt caching across Anthropic/OpenAI/Gemini with a stable + moving cache breakpoint, cost billed at the real cache-read discount, and the context meter driven by the driver's actual token usage -- so cost and context reflect reality and the status bar shows the dollars caching saved you. Savings-gated tool-output offload, absolute-token compaction advice, and optional per-turn output budgets (`+Nk` / `+Nk!`) cut waste without hiding results. |
| **Append-only context mode** | For local and cache-discounting providers, keeps the system prompt prefix byte-stable across turns and appends dynamic context in a trailer so provider KV caches reuse the heavy prefix -- real-dollar savings on long sessions. |
| **Cost transparency** | The status bar shows **process-wide** spend (pilot plus every delegated swarm/worker job in this backend process), priced at each job's actual model rate when usage is known. Unknown models fall back to the live OpenRouter price map (public `/models` feed, disk-cached), then to the router's pre-flight estimate -- never silently $0. The Swarm pane shows **per-repo session** spend on each job card (scoped to the active workspace), not a second copy of the status-bar total. |
| **Full-auto mode** | Unattended objective pursuit bounded by an AutoBudget governor (max swarms / tokens / seconds / idle), with a non-bypassable command safety guard. |
| **Command safety guard** | In full-auto, irreversible/remote/escalating shell commands (recursive deletes, ssh/scp, curl-pipe-to-shell, force-push, sudo, disk writes, key exfil) are screened and blocked; interactive co-working is untouched. Configurable per-command timeout (default 120s; 0/off = unbounded for long sessions). |

## Architecture

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

| Module | Role |
|--------|------|
| `harness/conversation.py` | The pilot loop: prose + structured tool calls -> execute -> feed real results back. Yields events for the GUI. |
| `harness/server.py` | stdlib HTTP server streaming Session events over SSE; CodeGraph status, wiki graph, settings, upload, sessions. |
| `harness/command_policy.py` | Pure, stdlib-only: timeout resolution + danger classification for the full-auto guard. |
| `harness/wiki_orchestrator.py` | Local-model structuring of a session digest into wiki pages (PM-free, testable). |
| `harness/state.py` | DurableState: clean read layer over Puppetmaster's SwarmStore (jobs, artifacts, live events). |
| `harness/config.py` | Swappable driver, reach, budget. |
| `webapp/` | Electron app (`electron/` main + preload + IPC bridges) and the React renderer (`src/`). |

## The research rig (heritage)

Marionette grew out of an eval that asked: **which LLMs can drive Puppetmaster as
a native harness layer?** That rig still lives in `pmharness/` and stays green:

| Module | Role |
|--------|------|
| `pmharness/intent.py` | DriverIntent contract + strict validator + lenient text->JSON parser. Pure; no PM dependency. |
| `pmharness/bridge.py` | Executes a validated intent against Puppetmaster's in-process Orchestrator (local adapter; deterministic, free). |
| `pmharness/drivers/` | Driver protocol: StubDriver (offline oracle), OpenAICompatDriver, plus Anthropic/Gemini drivers. |
| `pmharness/scoring.py` | Deterministic, no LLM-as-judge: json_valid, schema_valid, action_correct, executed_ok, composite. |
| `pmharness/ledger.py` | Append-only SQLite record of every attempt. |

Driver eval grades **driving**, not **working**: swarm intents execute on
Puppetmaster's free local adapter so ground truth is deterministic and key-free.

## Install and updates

Marionette runs from source, the way Hermes does. One installer works on macOS,
Linux, and Windows: it clones the repo, builds a per-machine Python venv with
`uv`, installs node deps, builds the renderer, and drops a `marionette` launcher
on your PATH. Native modules compile locally on your machine.

**macOS / Linux:**

```bash
curl -fsSL https://professorpalmer.github.io/marionette/install.sh | bash
```

**Windows (PowerShell):**

```powershell
irm https://professorpalmer.github.io/marionette/install.ps1 | iex
```

Fallback (raw GitHub URLs):

```bash
curl -fsSL https://raw.githubusercontent.com/professorpalmer/marionette/main/scripts/install.sh | bash
```

```powershell
irm https://raw.githubusercontent.com/professorpalmer/marionette/main/scripts/install.ps1 | iex
```

Then:

```bash
marionette            # daily use (built renderer, in-app updates)
marionette dev        # contributor hot-reload (Vite HMR)
marionette doctor     # re-check the environment
marionette update     # git pull + rebuild
```

### How distribution works (two paths, same app)

**Path 1 -- source installer (recommended for contributors and power users).**
The curl/irm installers above clone the repo, build a per-machine venv, and
drop a `marionette` launcher on your PATH. Updates are in-app: the status-bar
`update` pill runs `git pull` + rebuild + relaunch, so merging to `main` reaches
every source checkout on the next click. Because Marionette can edit its own
source, the updater stashes + reapplies local self-edits and flags a diverged
fork instead of failing silently.

**Path 2 -- thin Electron shell (optional).** GitHub Releases also ship signed
installers (DMG on macOS, `.exe` on Windows, AppImage on Linux). The packaged
app is a thin shell: on first launch it clones/bootstraps the same source tree
and venv, then runs the identical backend and renderer. You get a normal desktop
installer without a bundled Python runtime.

Both paths run from the same checkout after bootstrap. Version tags label what
you are on; see [RELEASING.md](RELEASING.md).

## Run it (contributor / dev)

Desktop app with Vite hot-reload for active editing:

```bash
cd webapp && npm install && npm run electron:dev
```

Or the one-command dev launcher (cleans stale processes, then launches):

```bash
bash scripts/dev.sh          # same as: marionette dev
```

Requirements: `git`, a C/C++ toolchain (Xcode Command Line Tools on macOS,
`build-essential` on Linux, Visual Studio Build Tools on Windows) for CodeGraph's
native module, and Node >= 20. `uv` provides Python; `marionette doctor` verifies
the whole environment.

Research rig (offline, no keys):

```bash
python3 -m venv .venv && .venv/bin/pip install -e /path/to/Puppetmaster pytest
.venv/bin/python -m pytest -q                              # full suite, offline
.venv/bin/python -m pytest -q -m full_auto_safety          # Wave 6 offline safety gate
.venv/bin/python scripts/run_eval.py --drivers stub-oracle # offline oracle ceiling
```

## Configuration

The driver and keys are set in the app (Settings pane) or via env. Key vars:

| Env var | Purpose |
|---|---|
| `OPENROUTER_API_KEY` | Default reach: the whole field through one endpoint. |
| `GEMINI_API_KEY` | Optional dedicated vision key. Not required -- vision also falls back to any provider key you already have that exposes a vision model. |
| `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` / `AWS_BEARER_TOKEN_BEDROCK` | AWS Bedrock BYOK (Settings can also load `~/.aws`). Pilots and agentic swarms use Converse + ConverseStream (live thinking/tool/text deltas); model pickers discover the account allow-list; prompt-cache hits feed the same token/cost/`cache_savings_usd` meters as Anthropic/OpenRouter. |
| `HARNESS_VLM_REACH` / `HARNESS_VLM_MODEL` | Explicit vision-sidecar override (e.g. `openrouter` for an open VLM) and its model. |
| `HARNESS_DRIVER` | Pilot model id. |
| `HARNESS_STATE_DIR` | State home for sessions, transcripts, prompt queue, keys. Defaults to a stable `~/.pmharness/state` so history survives restarts. |
| `HARNESS_COMMAND_TIMEOUT` | Per-command shell timeout in seconds; 0/off = unbounded. |
| `HARNESS_WORKER_TOKEN_BUDGET` | Default token ceiling for a single unsupervised worker run (default 40000). |
| `FIRECRAWL_API_KEY` | Optional. Enables the Firecrawl MCP catalog entry (State â†’ MCP); not used by native `web_fetch`. |
| `HARNESS_AUTO_COMMAND_GUARD` | Full-auto danger guard; default on, off to disable. |
| `HARNESS_WIKI_ORCHESTRATE` | Local wiki structuring: unset (off), 1/approve (prepare-and-approve), auto (silent ingest). |
| `HARNESS_AUTO_MAX_SWARMS` / `_TOKENS` / `_SECONDS` / `_MAX_IDLE` | Full-auto budget governor ceilings. |
| `HARNESS_APPEND_ONLY_CONTEXT` | Force append-only KV-cache context mode (auto-detected for local/cache-discounting endpoints when unset). |
| `HARNESS_COMPACTION_ADVISOR` | Surface layer-pressure compaction advice in `/api/usage` (default on). |
| `HARNESS_ADVISOR_COMPACTION` | Proactively run history compaction before the next turn once advice reaches level `now` (default on; set `0` to rely on the hard 75% trigger only). |

Swarm job costs in the UI come from measured usage priced against `~/.puppetmaster/models.json`, then the live OpenRouter `/models` map (cached under `~/.pmharness/or_models_cache.json`), then the router pre-flight estimate. Bedrock agentic workers use the same usage ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ `price_job` / tracker path (including cache-read discount). No manual registry entry is required for OpenRouter-hosted models like `z-ai/glm-5.2`.

## Conventions

- No emojis or decorative pictographs anywhere (code, docs, commits, output).
- stdlib-only backend; Puppetmaster is the single real dependency.
- `pmharness/intent.py` and `harness/command_policy.py` stay PM-free and pure so
  they unit-test fast and hermetically.
- Scoring is deterministic -- no LLM-as-judge.
- Tests before claiming done: `.venv/bin/python -m pytest -q`.
- Never commit keys or `results/*.sqlite`.
