# What Marionette does

## The app

Marionette is a three-pane desktop app: Electron, plus a Python backend
that streams events over SSE.

- **Center: the pilot loop.** A conversational driver. You talk to it. It
  emits structured tool calls (read, search, edit, run, delegate), and the
  harness runs them in code. No "I will now call the tool" narrator tax.
- **Right: tools on demand.** Closed by default, with a slim dock: Jobs,
  Economics, Review, Browser, Terminal, and State (CodeGraph, Wiki, MCP).
  The panel picker also opens Files, Git, Worktrees, and History. Open
  restores the last width on the chosen tab. Optional Firecrawl: set
  `FIRECRAWL_API_KEY`, then add Firecrawl from State > MCP (a catalog
  entry, not part of native `web_fetch`).
- **Left: workspace.** Projects, git branches and worktrees, sessions
  (named from the first message), and the Puppetmaster job list.

## Core capabilities

| Capability | What it does |
|---|---|
| **Provider-native pilot** | One driver, every OpenAI-compatible endpoint (OpenRouter or native), plus native Anthropic, Gemini, Bedrock, and Codex Responses drivers. Frontier models (Claude, GPT) and open-weights models (GLM, DeepSeek, Kimi, Qwen, MiniMax) drive the same loop. |
| **CodeGraph-first retrieval** | Per-turn structural context (symbols, defs, call sites) is injected before the model acts, so it leans on the graph and does not dump whole files. The index sees edits, additions, and deletions and refreshes in the background. |
| **Puppetmaster delegation** | `run_swarm`, `run_implement`, and `run_parallel` run bounded workers as durable jobs. `run_flow` hands Puppetmaster one graph for a multi-step pipeline (build, check, review, repair, or one task per item). It wakes the pilot only when the run is done, failed, stuck, stopped, interrupted, or waiting at a gate. `flow_control` answers gates and resumes, restarts, cuts, or stops a run. You can inspect recorded attempts, acceptance evidence, uncertain outcomes, and measured or estimated consumption. Marionette pins `puppetmaster-ai==1.50.0`. The installer and self-update use the same version. |
| **Portable LLM Wiki** | Durable memory across sessions and across LLMs. A local model structures a session digest into entity, concept, and decision pages (the "backwards" orchestration) at low cost, then ingests them. You approve by default. |
| **Durable memory graph** | Local durable facts and preferences (`MemoryStore`), plus optional relations through `MemoryGraph` (`GET /api/memory/graph`, shape `{nodes,edges}` like the wiki graph; sqlite + append-only jsonl). |
| **Vision on any driver** | A pilot that can see gets the real pixels. A text-only pilot gets a VLM sidecar transcription. The sidecar order: an explicit `HARNESS_VLM_REACH` override, then a Gemini or OpenRouter key, then any provider key you already have that exposes a vision model (Anthropic, OpenAI, xAI, and more). No separate vision key is needed if your provider can see. |
| **Honest token economics** | Prompt caching across Anthropic, OpenAI, and Gemini with a stable + moving cache breakpoint. Cost bills at the real cache-read discount. The context meter uses the driver's actual token usage, so cost and context match reality and the status bar shows the dollars caching saved you. Savings-gated tool-output offload, absolute-token compaction advice, and optional per-turn output budgets (`+Nk` / `+Nk!`) cut waste without hiding results. |
| **Append-only context mode** | For local and cache-discounting providers, keeps the system prompt prefix byte-stable across turns and appends dynamic context in a trailer, so provider KV caches reuse the heavy prefix. Real dollars saved on long sessions. |
| **Cost transparency** | The status bar shows process-wide spend: the pilot plus every delegated swarm or worker job in this backend process, priced at each job's actual model rate when usage is known. Unknown models fall back to the live OpenRouter price map (public `/models` feed, disk-cached), then to the router's pre-flight estimate. Never a silent $0. The Jobs pane shows per-repo session spend on each job card, not a second copy of the status-bar total. |
| **Encrypted sessions (opt-in)** | Encryption at rest for new sessions. See [Encrypted sessions](encrypted-sessions.md). |
| **Browser and native apps** | The pilot can drive the in-app browser and, with your grant, a running desktop app. See [Computer use](computer-use.md). |
| **Full-auto mode** | Unattended pursuit of an objective, bounded by the AutoBudget governor (max swarms, tokens, seconds, idle steps), with a command safety guard that cannot be bypassed. |
| **Scheduled autonomy** | `harness schedule` gives cron-backed local unattended runs with transactional cross-process claims, restart recovery, cooperative cancellation, truthful run history, and workspace safety. Fire times are host-local. HTTP and the Settings UI can list, change, run now, and show history (poll-first). The local daemon is still required for unattended cron fire. Per-schedule IANA timezone and schedule SSE are not done yet. |
| **Command safety guard** | In full-auto, irreversible, remote, or escalating shell commands (recursive deletes, ssh / scp, curl piped to a shell, force-push, sudo, disk writes, key exfil) are screened and blocked. Interactive co-working is untouched. The per-command timeout is configurable (default 120 s; `0` / `off` = unbounded). |

Every env var behind these is in [Configuration](../start/configuration.md).
