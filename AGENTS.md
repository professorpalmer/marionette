# AGENTS.md -- Marionette

Marionette is the product: a frontier/any pilot shell over a Puppetmaster
kernel. The `pmharness/` package is the research/eval rig that validated the
driver seam; it is not the shipping GUI contract.

## Product vs research

| Surface | Own it when… | Key modules |
|---|---|---|
| Product (lane B) | GUI/CLI pilot loop, tools, SSE, delegation | `harness/conversation.py` (turn loop only), `harness/pilot.py` (`PilotTurn` / schema), `harness/tool_dispatch.py`, `harness/tool_discovery.py`, `harness/pilot_guards.py`, `harness/hash_edit.py` |
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
  workflow is green on the target commit (both the 3.9 floor and 3.11 legs).
  The release workflow re-runs the suite as a hard gate before building
  installers, but do not rely on it -- check first, tag second. Local tests
  pass on the dev interpreter only; CI is what proves the 3.9 floor.
- Never commit keys or `results/*.sqlite`.

<!-- puppetmaster:rules:begin -->
<!-- managed by `puppetmaster install-rules`; delete this whole block to disable -->

# Puppetmaster orchestration

Puppetmaster is an MCP-based agent orchestrator with structured worker
swarms, durable SQLite state, tiered model routing, and zero-token
follow-ups via stored artifacts. When Puppetmaster's MCP server is
registered (`puppetmaster install-cursor-mcp` or
`puppetmaster install-codex-mcp`), the `puppetmaster_*` MCP tools are
available in this environment.

## Trigger convention (must obey)

When the user says **"Use Puppetmaster to …"**, **"PM this …"**, or
otherwise names Puppetmaster for a task, route that work through the
`puppetmaster_*` MCP tools — do not answer inline.

## Delegate-first gate (default path)

Before attempting multi-step work inline, start a Puppetmaster verb
(`puppetmaster_start_cursor_swarm`, `puppetmaster_start_swarm`,
`puppetmaster_start_implement`, or the matching sync verbs) when the
task is any of:

- Multi-file (3+ files) or cross-cutting refactor/migration
- An audit, review, or "find all X" search
- Work whose result will be reused later in this or a future session

Swarms and reviews run read-only analysis; building goes through
implement. Recall prior results with `puppetmaster_artifacts <job_id>`
at zero token cost.

Reach for a Puppetmaster verb **before** native broad search/exploration:
prefer `puppetmaster_codegraph_search` / `_context` over a repo-wide
`Grep`/`Glob`/`find`, and a swarm over the built-in `Task` tool, for any
multi-file investigation. When unsure whether a task qualifies, run the
classifier-backed gate — `puppetmaster_route_task` (or
`puppetmaster should-delegate "<prompt>"`) — which returns a delegate /
inline verdict and a suggested verb with zero LLM cost.

For deterministic enforcement, the user can install host hooks
(`puppetmaster install-hooks`) that inject this directive on prompt submit
and deny-redirect broad native exploration automatically. The kill switch
is `PUPPETMASTER_AUTO_INVOKE_DISABLED=1`.

## CodeGraph-first exploration (must obey)

CodeGraph is the default way to explore code — graph every directory you
interact with, then explore the graph instead of crawling the tree:

1. **Graph it first.** Before exploring any directory (the workspace root
   or a subtree you're diving into), check `puppetmaster_codegraph_status`;
   if it has no `.codegraph/`, run `puppetmaster_codegraph_init`
   (`index: true`) — it returns immediately and indexes in the background.
   Do not start grepping while you wait.
2. **Ask the graph, not the tree.** Resolve "where is X / what calls Y /
   what implements Z" with `puppetmaster_codegraph_search` /
   `_context` / `_affected` / `_files`, then `Read` only the files it
   points to.
3. **Partial coverage is still coverage.** CodeGraph indexes the languages
   it supports; unsupported files simply don't enter the graph. When part
   of the tree is ungraphable, still answer from the graph for everything
   it covers and scope native search narrowly to the ungraphed paths
   only — never re-crawl directories the graph already covers, and reuse
   that shared context instead of letting multiple workers/agents each
   re-explore the same graphed code.

Native search is fine for plain-text matches (log strings, config values,
comments), a single known file path, or when the user says "just grep".
If a codegraph MCP call returns a transport error, fall back to the CLI
passthrough `python -m puppetmaster codegraph …` — never a bare
`codegraph` from the shell (Node ABI mismatch).

## When NOT to use Puppetmaster (stay inline)

- Trivial single-file edits, typos, one-line fixes
- Quick factual questions
- Fast interactive iteration where the user is steering turn-by-turn

Routing those through Puppetmaster wastes tokens and latency.

## Fallback

If `puppetmaster_*` tools are not connected, fall back to native
tooling — do not pretend the tools exist.

## Usage

1. `puppetmaster_route_task <prompt> --role <role>` — dry-run that
   returns the chosen model, estimated cost, and reasoning. Use
   whenever spend matters or the task is ambiguous.
2. `puppetmaster_start_cursor_swarm` / `puppetmaster_start_swarm` for
   read-only analysis; `puppetmaster_start_implement` /
   `puppetmaster_start_claude_implement` / `puppetmaster_start_codex`
   for full-edit builds.
3. `puppetmaster_edit "<instruction>"` — a SINGLE focused in-place edit:
   cheapest sufficient model, CodeGraph to locate the site, edits the
   working tree directly, returns the diff synchronously, captures a
   reviewable PATCH. Prefer it over an inline single-file edit when the
   change benefits from CodeGraph or cheap-model routing; reserve
   `puppetmaster_start_implement` for coupled multi-file features (isolated
   worktree). Because it edits the live tree in place, `edit` is also the
   right verb for **last-mile work that builds on uncommitted changes**
   ("finish the module I just wrote", "add tests for the code I just
   added") — `puppetmaster_start_implement` branches off HEAD in a clean
   worktree and would never see that uncommitted work. Keep truly trivial
   edits (typo/rename/comment) inline.
4. `puppetmaster_artifacts <job_id>` — read structured outputs at zero
   token cost (results persist in SQLite).
5. `puppetmaster_dashboard [job_id]` — when the user asks to see/open
   the job dashboard, call this (it starts the local server if needed)
   and open the returned URL in a browser tab for them. CLI fallback:
   `python -m puppetmaster dashboard [job_id]`.
6. `puppetmaster_doctor` — sanity-check Puppetmaster's runtime
   dependencies once per session.

If `puppetmaster_doctor` reports critical failures, surface them to
the user before continuing.

<!-- puppetmaster:rules:end -->
