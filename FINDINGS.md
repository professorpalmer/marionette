# FINDINGS

## Stage 1 -- The PM-native driver seam (proven live)

**Result: no programmatic API needs building. It already exists and was driven
end-to-end in-process with zero MCP and zero CLI subprocess.**

### Layering
Puppetmaster's MCP tools and CLI are both thin transports over one engine:

- MCP `start_swarm` (`puppetmaster/mcp_server.py:2244`) builds `["run", goal,
  ...]` and shells out to the CLI via `start_cli(...)`.
- CLI `run` (`puppetmaster/cli.py:1894`) builds a store and calls
  `Orchestrator(store).run(...)`.
- `Orchestrator` (`puppetmaster/orchestrator.py:149`) is the real engine.

A native harness deletes both transports and calls the engine.

### The seam
```python
from puppetmaster.store_factory import create_store   # (backend, state_dir) -> SwarmStore
from puppetmaster.orchestrator import Orchestrator, RunResult

store  = create_store("sqlite", state_dir)
result = Orchestrator(store).run(
    goal, roles=None, specs=None,
    lease_seconds=5, worker_mode="subprocess",   # subprocess|inline|daemon
    on_job_created=callback,                      # fires when the job row exists
)
# RunResult(frozen): job, artifacts, summary, summary_path,
#                    recovered_tasks, rerouted_tasks, mode ("edit"|"analysis")
```

### Live proof
In-process temp SQLite store, local demo adapter: job ran to COMPLETE, the
`on_job_created` callback fired, 8 structured artifacts came back
(finding/decision/patch/risk/verification), stitched summary 1631 chars,
`store.list_artifacts()` re-read the same 8. The full driver loop --
start, follow, read structured state back -- works with no keys.

### Async question (resolved)
`Orchestrator.run()` is synchronous and blocks under every worker_mode
(`daemon` waits until settled). The live-observation seam is the store's event
layer: `read_events_since`, `event_cursor`, `wait_for_events` (push-style
long-poll), plus `list_jobs` / `list_artifacts` / `status_snapshot`. Harness
pattern: drive `run()` on a worker thread, observe via store events. No
architecture gap; no core change required.

### Implications for the MVP
1. The GUI is a thin read view over `SwarmStore` + the event stream.
2. The driver loop's contract is exact: emit a structured intent that maps to
   `Orchestrator.run` args; fold `RunResult.artifacts` back. That is the entire
   token-thesis mechanism, and it is concrete.
3. Puppetmaster runtime is pinned at `puppetmaster-ai==1.21.0` (CI,
   desktop bootstrap, and CONTRIBUTING); the old 0.9.x wiki note is obsolete.

## Stage 2 -- The driver eval rig (built, green offline)

10-task labeled battery (swarm/answer/stop). Deterministic scoring, SQLite
ledger. `stub-oracle` control scores 100% driving real Puppetmaster. Open-
weights (Kimi, GLM) and frontier (OpenAI) rows are wired and pending keys.

Next data point: run the real drivers and record valid-call rate, decision
accuracy, tokens, and latency per model -- the first rows of the "ideal harness
model" research and the cost-thesis receipt.


## Stage 2 -- Single-turn driver eval (LIVE, full field)

10 models scored on the labeled battery vs real Puppetmaster. Result table in
results/STAGE2_RESULTS.md. Headline: 7 open-weights models tied the Claude Opus
control at 100%; qwen3-coder-30b drove at $0.00044/run (~100x cheaper than
Claude) with identical single-turn score. kimi-k2.6 scored 70% -- a reasoning
model emitting prose instead of bare JSON on trivial cases (harness needs
JSON-mode for thinking models; fixable, not a capability ceiling).

CAVEAT: single-turn battery SATURATED (8/10 at exactly 100%) -- proves the floor,
does not rank. Total field spend ~8 cents.

## Vision capability gap + the Kimi paradox

Verified on HF task types: among open DRIVERS, only Kimi (image-text-to-text) has
native vision; GLM-5.2, MiniMax-M2.7, DeepSeek-V4, Qwen3-Coder are text-only.
The paradox: the ONLY vision-capable open driver is also the WEAKEST driver
(Kimi 70%). Vision and driving quality are anti-correlated in the current field.

Architectural resolution (catalog vision flag + vision_sidecar tier): DECOUPLE
vision from the driver. A cheap open VLM sidecar (GLM-OCR / Kimi-VL / Qwen-VL)
transcribes image -> text artifact in durable state; any text-only driver then
consumes the text. This keeps the best DRIVER usable while still accepting
images -- matching the kernel philosophy (harness orchestrates capabilities;
driver decides). Image processed once, stored, never re-sent through every call.

## Stage 3 -- Multi-turn driving eval (LIVE)

The discriminating eval. Driver acts -> PM executes -> artifacts fed back ->
decide next -> terminate or turn guard. Trajectory scoring: terminated /
correct terminal action / efficient / all-valid / grounded. Table in
results/STAGE3_RESULTS.md.

KEY FINDING: multi-turn discriminates where single-turn saturated. The
differentiator is TERMINATION + EFFICIENCY (knowing when to stop), not JSON
validity (100% everywhere). Counter-intuitively, claude-opus-4-8 (perfect
single-turn) scored WORST multi-turn (55%) -- it never terminated two
'investigate' episodes, looping the swarm 6x instead of concluding. This is the
loop-burn failure mode a PM-native harness must defend against, now measurable.

CONFOUND (Stage 3.5): absolute scores are sensitive to harness feedback design
(demo artifacts are thin, so careful models keep digging). The eval MECHANISM is
proven; a definitive leaderboard needs a richer real-artifact substrate + an
explicit turn/cost budget signal. Do not over-read the absolute ranking yet.


## Stage 3.5 -- Budget-aware multi-turn (LIVE): the confound, confirmed

Fixed Stage 3's three confounds: substantive findings substrate (gold-fixture,
real PM still executes), explicit orchestration budget in the system prompt, and
vague/explicit prompt variants; sharper scorer penalizes budget overrun AND
premature stop. Table in results/STAGE3_5_RESULTS.md.

RESULT: claude-opus-4-8 went 55% (Stage 3) -> 100% (Stage 3.5). The loop-burn
vanished with a real findings digest + an explicit budget. Gemini 100% too. This
CONFIRMS the Stage 3 confound: Claude was never a bad driver; the thin
demo-artifact substrate was starving it, and a careful model correctly kept
digging under a broken signal.

LESSON (pro-thesis): driving quality is as much harness feedback design + budget
signaling as model choice. A well-designed harness makes even frontier models
behave economically -- the orchestration layer is a lever on cost/behavior,
which argues FOR a purpose-built PM-native harness.

LIMIT -> Stage 4: V2 battery now saturated for frontier models (100% across).
It de-confounded but does not rank. Next: harder multi-swarm episodes (budget
3+, min_swarms 2) and genuine stop-early-correct vs premature traps.


## Stage 4 + cross-check: the real open-weights leaderboard (LIVE, funded)

With a funded OpenRouter key, ran the full open-weights field on BOTH the Stage 4
read-decide ranking traps and the Stage 3.5 budget battery. First runs that RANK
instead of saturate.

Stage 4 (read-decide): qwen3-coder-30b 100% (535 tok) > glm-5.2 100% (974) >
glm-4.7-flash 100% (3577) > deepseek-v4-flash 90% > minimax-highspeed 90% >
minimax-m2.7 81.7% > deepseek-v4-pro 81.7% > kimi-k2.6 53.3% (4809 tok, 68s/call).

The failure mode the eval caught is real: losers fail the CONCLUSIVE trap with
sw=0, prem=True -- they stop/answer WITHOUT investigating a task whose findings
only resolve after a swarm. Premature pattern-matching, exactly what the trap was
built to expose. Kimi (verbose reasoner) is the consistent loser: most tokens,
slowest, worst judgment here.

Cross-check (Stage 3.5 budget): qwen 100%/607 > glm-5.2 100%/984 > deepseek-flash
100% > ... > kimi 93.1% > minimax-highspeed 74.4%. Top stable, bottom wobbles.

DECISION: default driver = qwen3-coder-30b (100% both batteries, lowest tokens,
Apache-2.0). glm-5.2 the MIT alternative. Cost thesis CONFIRMED on the
discriminating eval with real open weights: the cheapest, cleanest-licensed open
model drives Puppetmaster as well as any, and better than the expensive reasoner.

LIVE GUI: drove the harness on qwen3-coder-30b through the browser -- a real
multi-turn task (investigate durable state -> narrow -> conclude) ran 2 real PM
jobs, 10 artifacts, 9 turns rendered, no JS errors. Open weights driving real
Puppetmaster in the Cursor-3.0-style UI, end to end.
