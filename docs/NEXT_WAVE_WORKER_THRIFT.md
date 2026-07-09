# Next wave: Worker thrift — compaction + tool-output offload for swarm subprocesses

**Status:** OPEN — deferred after Puppetmaster **v1.13.0** (caching lifts shipped; compaction/offload did not).  
**Audience:** fresh agent session with a clean context window.  
**Primary repo:** `C:\Users\pwall\Projects\Puppetmaster` (agentic worker loop).  
**Companion UX wave:** `docs/NEXT_WAVE_MULTI_SESSION.md` (Marionette) — orthogonal; can ship in parallel with disjoint files.  
**Goal:** Close the remaining gap vs Claude Code / oh-my-pi on **worker** token thrift so swarm subprocesses inherit the same savings-gated compaction discipline the Marionette pilot already has.

Do not re-litigate prompt-cache breakpoints or static-first prefixes — those shipped in v1.13.0.

---

## Origin (why this wave exists)

User question (2026-07-08): *“we dont get the huge caching and compaction advantages when deploying swarm subprocesses? … best poor mans harness … Claude-level parity?”*

Honest answer at the time: **pilot yes / workers half.** Two implement jobs were fired:

| Job | Lift | Outcome |
|-----|------|---------|
| A | Anthropic `cache_control` in `provider_chat` | **Shipped** v1.13.0 (`b7fb660`) |
| B | Static-first shared-prefix prompt assembly | **Shipped** v1.13.0 (`e7b2749`) |

Items **3–5** below were ranked next and **never implemented**. This doc is only those leftovers.

Transcript anchor: chat `bf62d89f-c126-48a4-89c0-ceb3a63c98ad` (~6:52–7:00pm Jul 8).

---

## What already shipped (do not redo)

### Puppetmaster v1.13.0 — caching inheritance for workers

| Piece | Where | Effect |
|-------|-------|--------|
| `cache_control` on system + tools + 2 rolling history markers | `puppetmaster/providers.py` | Anthropic-direct agentic multi-turn near-flat like the pilot |
| Kill switch | `PUPPETMASTER_PROMPT_CACHE=0` | Opt out |
| `cache_write_tokens` usage parse | providers usage path | Honest accounting |
| Static-first prompts (boilerplate → task last) | `puppetmaster/adapters/_prompts.py` + adapters | Cross-worker shared prefix for implicit + explicit caches |
| `insert_before_task` / `split_prompt_messages` | prompts + agentic | CodeGraph/memory/skills inject before task section |
| CodeGraph context `lru_cache` | `puppetmaster/codegraph.py` | Softens per-process cold re-query |

### Marionette pilot (already had full thrift — not this wave)

- Anthropic driver breakpoints, 0.1x cache-read billing, savings-gated tool-output offload (`harness/tool_output_savings.py`), compaction advisor, status-bar `cache_saved_usd` / `tool_output_savings_usd` / `cache_saved_usd_swarm`.

### Explicitly out of scope here

- Multi-session / SSE detach / runner registry → `NEXT_WAVE_MULTI_SESSION.md`
- Platform lock / SSRF / webview → done in v0.9.17 / v1.13.0
- Changing Cursor-adapter economics (plan-billed; marginal ~$0 already)

---

## The remaining gap (file-grounded)

**Blunt truncation, not savings-gated offload:**

```140:140:puppetmaster/adapters/agentic.py
_TOOL_OUTPUT_LIMIT = 12000  # per tool result, chars, before truncation note
```

Every tool result path in the agentic loop `_truncate`s to 12k chars. The pilot’s Round 10 pattern (full body → artifact, head+tail in context, dollars saved to ledger) **does not reach workers**.

**No history compaction for long implement workers:** after many turns, old tool results stay in the message list (subject only to per-result truncation). Pilot has a compaction advisor; workers do not.

**Job-level CodeGraph brief is only partial:** process-local `lru_cache` helps identical queries in one process; siblings still don’t share one job-scoped “repo brief” artifact computed once at swarm start.

---

## Ranked work (ship in this order)

### 1. Savings-gated tool-output offload in the agentic loop (highest impact)

**Port the Marionette / OMP Round 10 pattern into `puppetmaster/adapters/agentic.py`:**

1. On large tool results: write full text to a durable artifact (or job-scoped blob under state dir).
2. Inject **head + tail** (or structured summary) into the model message, with a pointer to the artifact id.
3. Record estimated tokens avoided + USD saved into the existing savings ledger so `python -m puppetmaster savings` and Marionette’s `tool_output_savings_usd` / swarm fields stay honest.
4. Keep a kill switch (e.g. `PUPPETMASTER_TOOL_OFFLOAD=0`) and a size threshold so tiny results stay inline.
5. Replace call sites that use `_truncate(..., _TOOL_OUTPUT_LIMIT)` for model-facing content; keep truncation only as a last-resort fallback.

**Reference implementation:** `marionette/harness/tool_output_savings.py` + pilot wiring. Prefer reuse/adapt over inventing a third scheme.

**Tests:** threshold fires / does not fire; artifact present; message content is head+tail not full; ledger entry shape; kill switch disables.

### 2. History compaction for long implement workers

After **N** turns (or when estimated context tokens exceed a budget):

- Collapse older tool-result messages to one-line stubs (`ok: path edited`, `exit=0`, etc.).
- Preserve recent K turns verbatim.
- Never compact the static system/prefix block (must stay cache-stable — v1.13.0 invariant).
- Disposable worker transcripts: simpler than the pilot advisor is fine; deterministic rules beat LLM-as-judge.

**Tests:** after N synthetic turns, older tool payloads shrink; prefix bytes before task header unchanged; kill switch / env floor.

### 3. Per-job shared CodeGraph / repo brief (cold-start)

- At swarm/job start, compute one **job-level** context block (repo census + CodeGraph brief for the goal).
- Stamp it into every worker prompt via the existing `insert_before_task` seam so all siblings share an identical prefix segment.
- Process `lru_cache` stays as a micro-optimization; this is the cross-worker / cross-process piece.

**Tests:** two tasks in one job receive identical brief bytes; brief sits before task instruction header.

---

## Suggested worker split (collision-safe)

| Worker | Owns | Must not touch |
|--------|------|----------------|
| A — tool offload | `adapters/agentic.py` tool-result paths; new small helper module e.g. `puppetmaster/tool_offload.py`; savings ledger hooks; tests | `_prompts.py` static-first order, `providers.py` cache_control |
| B — history compaction | agentic loop turn bookkeeping / message rewrite; tests | offload helper internals (call the API only) |
| C — job-level brief | job/orchestrator start path + prompt injection; `codegraph.py` only if needed | agentic truncation constants |

Commit between workers. Do not leave uncommitted live-tree edits in files a running implement worker’s baseline includes.

---

## Release discipline

- Primary ship target: **Puppetmaster** (version bump → CI green → tag → GitHub release → **twine** to PyPI).
- Marionette: only if status-bar / usage API needs new fields to surface worker offload savings (often already covered by swarm savings aggregation — verify before bumping).
- Wiki ingest the decision when done.
- Usage-driven: ship when these land; do not invent a round number for cadence.

---

## Success criteria (done when)

- [ ] Agentic Anthropic (and OpenAI-compat) workers no longer keep full multi-12k tool dumps in context by default; large outputs offload with ledger credit.
- [ ] Long implement workers compact older tool turns without breaking the static cacheable prefix.
- [ ] Sibling workers in one swarm share one job-level CodeGraph/repo brief prefix segment.
- [ ] `puppetmaster savings` (and Marionette status bar if applicable) show the new savings class — measured, not asserted.
- [ ] Kill switches work; full pytest green; CI green before tag.

---

## Ranking claim (after this wave + v1.13.0)

**Before this wave (today):** top-tier on auditability + routing + pilot thrift; worker caching largely closed in v1.13.0; still behind Claude Code / best-in-class on **worker compaction discipline**.

**After this wave ships:** for “most outcome per $1 of tokens” among open / BYOK harnesses, Marionette+Puppetmaster should sit in the **top tier with a straight face** — matching or beating Aider/oh-my-pi-style thrift on workers while keeping measured savings receipts and cheap-model routing those tools lack. Still not a claim of beating Claude Code’s proprietary end-to-end product UX; it **is** a claim of budget-harness parity on the token-economics mechanisms that matter for swarm subprocesses.

Do not market “#1” until the three checkboxes above are green and you’ve watched real swarm receipts for a few days.

---

## One-line brief for the next session

> Port Marionette’s savings-gated tool-output offload and a simple history-compaction pass into Puppetmaster’s agentic worker loop; add a job-level shared CodeGraph brief; leave v1.13.0 cache_control and static-first prompts alone; ship via PyPI when CI is green.
