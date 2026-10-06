# AGENTS.md -- Marionette

Marionette is the product: a frontier/any pilot shell over a Puppetmaster
kernel. The `pmharness/` package is the research/eval rig that validated the
driver seam; it is not the shipping GUI contract.

## Product vs research

| Surface | Own it when… | Key modules |
|---|---|---|
| Product (lane B) | GUI/CLI pilot loop, tools, SSE, delegation | `harness/conversation.py` (facade) + mixins (`send_loop`, `busy_control`, …), `harness/api/*` HTTP peels, `harness/pilot.py` (`PilotTurn` / schema), `harness/tool_dispatch.py`, `harness/tool_discovery.py`, `harness/pilot_guards.py`, `harness/hash_edit.py` |
| Research rig | DriverIntent eval, scoring, Stage batteries | `pmharness/intent.py`, `pmharness/bridge.py`, `pmharness/drivers/`, `harness/session.py` (single-shot) |

Ownership rule: new pilot tools -> `pilot.py` schema + `tool_dispatch` /
`tool_discovery`; new orchestration -> Puppetmaster; do not grow
`conversation.py` with per-tool handlers.

## Conventions

- No emojis or decorative pictographs anywhere (code, docs, commits, output).
  Plain words only.
- stdlib-only for the harness/rig itself (urllib, sqlite, dataclasses).
  Puppetmaster is the single real dependency, installed editable from the local
  checkout.
- The `pmharness/intent.py` layer must stay PM-free and pure so it unit-tests
  fast and hermetically. Execution coupling lives only in `bridge.py`.
- Scoring is deterministic -- no LLM-as-judge. Every metric must be a function
  of (labeled task, raw driver text, execution result).
- Driver eval measures driving, not working: swarm intents execute on
  Puppetmaster's free local adapter for deterministic ground truth.
- Tests before claiming done: `.venv/bin/python -m pytest -q`. The offline E2E
  test drives real Puppetmaster and must stay green with zero API keys.
- Releases only from green CI: never push a release tag until the `tests`
  workflow is green for this git tree (3.9 floor, 3.11, Windows, frontend-build).
  If dev-into-main `merge^{tree}` equals the dev PR tree that already passed,
  tag immediately -- do not wait for main to run the same suite again. The
  release workflow must not re-run pytest; it only checks that a successful
  `tests` run exists for the tree, then publishes installers. Local tests
  pass on the dev interpreter only; CI is what proves the 3.9 floor.
- Never commit keys or `results/*.sqlite`.
- Git flow: day-to-day work is on `dev`. Feature PRs target `dev`. Ship by
  merging `dev` into `main` (dev contains `main` first), tagging when the
  dev-into-main PR `tests` matrix is green and `merge^{tree}` matches, then
  pushing `vX.Y.Z` on `main`. Never push product work or `pmedit-*` worker
  branches to `main` / origin scratch.

<!-- puppetmaster:rules:begin -->
<!-- managed by `puppetmaster install-rules`; delete this whole block to disable -->

# Puppetmaster orchestration

Puppetmaster runs durable worker jobs and flow graphs for you through the
`puppetmaster_*` MCP tools: workers that survive restarts, per-item check
and repair, cheap worker models under an expensive pilot, and follow-ups
that resume a worker's own session.

## Are you a Puppetmaster worker? (check this first)

**If `PUPPETMASTER_WORKER` is `1`, or Puppetmaster issued your prompt,
every delegation rule below is void for you.** You *are* the worker it
delegated to. Do the analysis or the edit yourself and return the
artifacts your prompt asks for.

You are a Puppetmaster worker if `PUPPETMASTER_WORKER=1` in the
environment, or your prompt contains a `Puppetmaster artifact contract:`
block, a `Role: <role>` + `Goal: <goal>` header, or an instruction to
finish by calling `submit_findings` / `submit_report`. Nested job starts
are refused while that env is set (override:
`PUPPETMASTER_ALLOW_NESTED=1`). Workers run as plain agent CLIs with **no
`puppetmaster_*` MCP tools**, so delegating is impossible. Use your own
native tools.

## Trigger convention (must obey)

When the user says **"Use Puppetmaster to ..."**, **"PM this ..."**, or
otherwise names Puppetmaster for a task, route that work through the
`puppetmaster_*` MCP tools rather than answering inline.

## Solo first; fan out when it pays

Do the work yourself unless parallel workers clearly finish sooner or
better. One session that holds the whole problem beats any fan-out on
small work: every worker pays a fixed start (its own context, reading,
checks) and you pay to integrate. Fan out when the work splits into
independent units that each take a worker minutes, or when there is more
of it than you can finish well in one session (you are running long, or
your context is filling with work that does not depend on itself). Many
small units you could write in a few minutes are still solo work.

- Exact edits, typos, small follow-ups and revisions: make them yourself.
  If your instruction to a worker would spell out the change, it is
  cheaper to make it.
- A revision that needs a prior worker's context: continue its flow run
  with `continue_from`, or resume the worker with `resume_from`, instead
  of starting fresh sessions.
- Marionette decides this for you from your plan and measured pace; on
  other hosts, `puppetmaster sizing` gives the same decision.

## Fan out with one flow

Write ONE flow graph and start it with `puppetmaster_flow` (action
`run`), then call action `wait` (or end your turn). Puppetmaster walks it
durably and wakes you only when it is done, failed, stuck, interrupted or
waiting at a gate; do not launch, poll and hand off each step yourself.
The tool description has the node shapes and a fan-out example.

- **Size workers to the work.** Group many small units into a few `map`
  items (each an object with its unit names and files; 16 small modules:
  3-5 workers) and set concurrency to the number of items so they all
  run at once. Give a unit its own worker only when it is minutes of work.
- **Check each unit.** Put the unit's own check (`shell`) after its build
  with a `fail` edge back to the build (`max` 2), inside the map item, so
  each unit is repaired alone.
- **Armor for craft.** When the result is judged by how it looks, reads
  or feels (visuals, geometry, UI, prose) and not only by a test, add a
  `shell` step that produces the observable result (render, run,
  screenshot) and a `judge` whose task is a numbered rubric from the
  user's request, with its FAIL edge back to the build (`max` 2).
  Passing tests is not the bar a user grades.
- Leave the model unpinned unless the user named one: workers then run
  the model you are configured with.

## Label every job you start (do it by default)

When you start any job verb (`puppetmaster_start_*`, `puppetmaster_edit`,
or the matching sync verbs), pass a short human-readable `label` (3-6
words, e.g. `"auth refactor audit"`). It becomes the job's headline on the
dashboard and in `puppetmaster_jobs`.

## CodeGraph for unfamiliar code

When you must find where something is, what calls it or what a change
affects in code you have not read, ask the graph instead of crawling the
tree: `puppetmaster_codegraph_status`, then `puppetmaster_codegraph_init`
(`index: true`) if there is no index, then `puppetmaster_codegraph_search`
/ `_context` / `_affected`, and read only the files it points to. Skip it
for a small repository you can list at a glance, for files you already
know, and for plain-text matches (log strings, config values). If a
codegraph MCP call fails, use `python -m puppetmaster codegraph ...`,
never a bare `codegraph` from the shell.

## Fallback

If a `puppetmaster_*` observation tool is not connected, continue the same
durable job through `python -m puppetmaster status|await|show <job_id>`.
Check recent jobs before retrying a start; a dropped MCP reply is not proof
that no job was created. Use native tooling only when no Puppetmaster job
exists and the task itself permits inline work.

## Other verbs

- `puppetmaster_start_swarm` for read-only analysis across several
  lenses; `puppetmaster_edit` for one focused in-place edit that builds on
  uncommitted work; `puppetmaster_start_implement` for one coupled change
  in an isolated worktree. With a provider API key but no vendor CLI
  (keys-only), use `puppetmaster_agentic` / `puppetmaster_start_agentic`.
- Every asynchronous `start_*` response is a resumable contract: follow
  its returned `monitor_with` tool with the exact `job_ref`. Treat only
  `delivery.verdict == "delivered"` as success.
- `puppetmaster_artifacts <job_id>` reads stored results at zero token
  cost; `puppetmaster_route_task` previews the routed model and price when
  spend matters; `puppetmaster_dashboard` opens the job dashboard when the
  user asks.
- `puppetmaster_doctor` when a Puppetmaster call fails or behaves
  unexpectedly; surface critical failures to the user. Not a ritual.

<!-- puppetmaster:rules:end -->
