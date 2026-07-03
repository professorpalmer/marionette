# Marionette

A desktop AI coding harness where the LLM is a **component inside** the kernel,
not the platform. Marionette drives any model -- frontier or cheap open-weights --
through a structured pilot loop over [Puppetmaster](https://github.com/professorpalmer/puppetmaster)
durable state, with CodeGraph-aware retrieval, a portable cross-session knowledge
wiki, and multi-worker delegation.

Internal-first research rig and daily-driver app. stdlib-only backend (urllib +
sqlite); Puppetmaster is the one real dependency, installed editable from a local
checkout.

> Status: v0.6.x, deliberately pre-1.0. Vetted privately before any wider release.

## What it is

Marionette is a three-pane desktop app (Electron + a stdlib Python backend over
SSE):

- **Center -- the pilot loop.** A conversational driver: you talk to it, it emits
  structured tool calls (read, search, edit, run, delegate), and the harness
  executes them in code. No "I will now call the tool" narrator tax.
- **Right -- durable state.** Live CodeGraph stats, the portable wiki graph, and
  the artifact feed from every action and delegated job.
- **Left -- workspace.** Projects, git branches/worktrees, sessions (auto-named
  from the first message), and the Puppetmaster job list.

The thesis: there is no single best model. Hot-swap the driver by task. A cheap
local model (e.g. qwen3-coder-30b via OpenRouter, cents per session) handles
inline work; heavier reasoning and multi-file changes delegate to Puppetmaster
workers the router selects.

## Core capabilities

| Capability | What it does |
|---|---|
| **Provider-native pilot** | One driver, every OpenAI-compatible endpoint (OpenRouter or native). Frontier control models (Claude, GPT) and open-weights (GLM, DeepSeek, Kimi, Qwen, MiniMax) drive the same loop. |
| **CodeGraph-first retrieval** | Per-turn structural context is auto-injected (symbols, defs, call sites) before the model acts, so it leans on the graph instead of dumping whole files. Self-healing: the index detects edits, additions, and deletions and refreshes in the background. |
| **Puppetmaster delegation** | run_swarm (read-only analysis), run_implement (edit-capable worktree worker), run_parallel (concurrent waves). Heavy/multi-file work runs as durable, auditable jobs. |
| **Portable LLM Wiki** | Cross-session, cross-LLM durable memory. A local model structures a session digest into entity/concept/decision pages (the "backwards" orchestration) cheaply, then ingests them -- human-approved by default. |
| **Vision on any model** | Paste or drop a screenshot; a VLM sidecar (Gemini, OpenRouter fallback) transcribes it so even a non-vision driver "sees" the image. |
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

### Download the desktop app

Prefer a click-to-install build? Grab a signed/notarized installer
(`v0.6.42`, [all releases](https://github.com/professorpalmer/marionette/releases/latest)):

- macOS (universal, Intel + Apple Silicon): [Marionette-0.6.42-universal.dmg](https://github.com/professorpalmer/marionette/releases/download/v0.6.42/Marionette-0.6.42-universal.dmg)
- Windows: [Marionette-0.6.42-Setup.exe](https://github.com/professorpalmer/marionette/releases/download/v0.6.42/Marionette-0.6.42-Setup.exe)
- Linux: [Marionette-0.6.42.AppImage](https://github.com/professorpalmer/marionette/releases/download/v0.6.42/Marionette-0.6.42.AppImage)

The packaged app is a thin shell: on first launch it bootstraps the same source
checkout into `~/.marionette/marionette`, so it stays self-updating like the
installer path.

Updates are in-app: the status-bar `update` pill runs `git pull` + rebuild +
relaunch. Merging to `main` reaches everyone on their next click. Because
Marionette can edit its own source, the updater knows how to stash + reapply
local self-edits and flags a diverged fork instead of failing silently. Cutting a
version tag triggers signed/notarized release builds; see `RELEASING.md`.

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
`build-essential` on Linux) for CodeGraph's native module, and Node >= 20. `uv`
provides Python; `marionette doctor` verifies the whole environment.

Research rig (offline, no keys):

```bash
python3 -m venv .venv && .venv/bin/pip install -e /path/to/Puppetmaster pytest
.venv/bin/python -m pytest -q                              # full suite, offline
.venv/bin/python scripts/run_eval.py --drivers stub-oracle # offline oracle ceiling
```

## Configuration

The driver and keys are set in the app (Settings pane) or via env. Key vars:

| Env var | Purpose |
|---|---|
| `OPENROUTER_API_KEY` | Default reach: the whole field through one endpoint. |
| `GEMINI_API_KEY` | Vision sidecar (image transcription); OpenRouter VLM fallback. |
| `HARNESS_DRIVER` | Pilot model id. |
| `HARNESS_COMMAND_TIMEOUT` | Per-command shell timeout in seconds; 0/off = unbounded. |
| `HARNESS_AUTO_COMMAND_GUARD` | Full-auto danger guard; default on, off to disable. |
| `HARNESS_WIKI_ORCHESTRATE` | Local wiki structuring: unset (off), 1/approve (prepare-and-approve), auto (silent ingest). |
| `HARNESS_AUTO_MAX_SWARMS` / `_TOKENS` / `_SECONDS` / `_MAX_IDLE` | Full-auto budget governor ceilings. |

## Conventions

- No emojis or decorative pictographs anywhere (code, docs, commits, output).
- stdlib-only backend; Puppetmaster is the single real dependency.
- `pmharness/intent.py` and `harness/command_policy.py` stay PM-free and pure so
  they unit-test fast and hermetically.
- Scoring is deterministic -- no LLM-as-judge.
- Tests before claiming done: `.venv/bin/python -m pytest -q`.
- Never commit keys or `results/*.sqlite`.
