# Configuration

Set the driver and keys in the app (Settings pane) or with environment
variables. Most people never touch env vars: paste a key in Settings and go.

## Environment variables

| Env var | Purpose |
|---|---|
| `OPENROUTER_API_KEY` | Default Full stack key: chat pilot and agentic workers through one endpoint. No other platform is required. |
| `GEMINI_API_KEY` | Optional dedicated vision key. Not required: vision also falls back to any provider key you already have that exposes a vision model. |
| `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` / `AWS_BEARER_TOKEN_BEDROCK` | AWS Bedrock BYOK (Settings can also load `~/.aws`). Pilots and agentic swarms use Converse + ConverseStream (live thinking, tool, and text deltas). Model pickers discover the account allow-list. Prompt-cache hits feed the same token, cost, and `cache_savings_usd` meters as Anthropic and OpenRouter. |
| `HARNESS_VLM_REACH` / `HARNESS_VLM_MODEL` | Explicit vision-sidecar override (`openrouter` for an open VLM, or `gemini`) and its model. The OpenRouter default model is `qwen/qwen3-vl-30b-a3b-instruct`. |
| `HARNESS_DRIVER` | Pilot model id. |
| `HARNESS_COMPACTION_MODEL` | Optional cheaper summarizer for the hybrid and summary residuals. Empty, the live pilot, or a local host all stay extractive. |
| `HARNESS_STATE_DIR` | State home for sessions, transcripts, prompt queue, and keys. Defaults to `~/.pmharness/state`, so history survives restarts. |
| `HARNESS_COMMAND_TIMEOUT` | Per-command shell timeout in seconds. Default 120. `0` / `off` = unbounded. |
| `HARNESS_COMMAND_HARD_CEILING` | Safety ceiling in seconds when the command timeout is unbounded. Default 900. `0` / `off` disables it. Background commands also run to this ceiling. |
| `HARNESS_WORKER_TOKEN_BUDGET` | Default token ceiling for one unsupervised worker run. Default 250000. `0` / `off` / `unlimited` = no per-worker cap. Values from 1 through 39999 reset to 250000. |
| `FIRECRAWL_API_KEY` | Optional. Turns on the Firecrawl MCP catalog entry (State > MCP). Native `web_fetch` does not use it. |
| `HARNESS_JEV` | Opt-in Jev turn judgment. Default off. Only `1` / `true` / `on` / `yes` turn it on. Never required. |
| `HARNESS_COMPACTION_RESIDUAL` | Compact residual. Default `catalog` (also the fallback for an empty or invalid value). Settings cycle: catalog, hybrid, summary. `off` is env-only. |
| `HARNESS_LOCAL_TURN_SPLIT_STEPS` | Local-model pilots compact a long tool loop every N steps. Default `0` = never. Context fill still compacts at 75% of the window. Cloud pilots keep one unbroken turn. |
| `HARNESS_COMPACTION_VAULT` | SQLite FTS retrieve of compacted history. Default on. `0` disables the inject. |
| `HARNESS_AUTO_COMMAND_GUARD` | Full-auto danger guard. Default on. `off` disables it. |
| `HARNESS_BROWSER_REAL_PROFILE` | Consent-gated copy of the last-used Chrome / Chromium login data into `~/.pmharness/browser-profile-real`. Default off. Settings > Safety: Use my Chrome login. |
| `HARNESS_WIKI_ORCHESTRATE` | Local wiki structuring: unset (off), `1` / `approve` (prepare, then you approve), `auto` (silent ingest). |
| `HARNESS_AUTO_MAX_SWARMS` / `_TOKENS` / `_SECONDS` / `_IDLE` | Full-auto budget governor ceilings. Defaults: 20 swarms, 500000 tokens, 3600 s, 3 idle steps. For tokens, `0` / `off` / `unlimited` skips the token halt. |
| `HARNESS_APPEND_ONLY_CONTEXT` | Force append-only KV-cache context mode. When unset, it is detected for local and cache-discounting endpoints. |
| `HARNESS_COMPACTION_ADVISOR` | Show layer-pressure compaction advice in `/api/usage`. Default on. |
| `HARNESS_ADVISOR_COMPACTION` | Compact history before the next turn once advice reaches level `now`. Default on. `0` keeps only the hard 75% trigger. |

## Compact residual

After a long chat, Marionette compresses older turns so the next prompt
stays inside the context window. The factory residual is **catalog**: an
extractive index of files, tools, handles, and a last-wins selected story,
plus a local SQLite vault that retrieves matching slices on later asks.

Settings > General can opt into **hybrid** or **summary**. Those paid
paths use a cheaper summarizer only when `HARNESS_COMPACTION_MODEL` names a
model that is not the live pilot and not a local host. Otherwise they stay
extractive. The session pilot is never asked to compact. `off` is env-only.
An empty value never means `off`.

The lab and paper for this design live in a separate repo:
[Extractive Residuals After Compaction](https://professorpalmer.github.io/catalog-residual/).

## Jev turn judgment (opt-in)

Off by default. Turn it on in Settings > General > Opt-ins, or with
`HARNESS_JEV=1`. When it is on and an OpenRouter key is already stored, one
Decisions call can pick which skill body to load. The harness does not need
Jev and does not need an OpenRouter key. Empty, `auto`, and `0` stay off.
Without a key the switch is idle and retrieve stays on token overlap.

## How swarm job costs are priced

Swarm job costs in the UI come from measured usage, priced in this order:

1. `~/.puppetmaster/models.json`
2. the live OpenRouter `/models` map, cached under `~/.pmharness/or_models_cache.json`
3. the router pre-flight estimate

Bedrock agentic workers use the same usage to `price_job` to tracker path,
with the cache-read discount. OpenRouter-hosted models such as
`z-ai/glm-5.2` need no manual registry entry.

## Reasoning budget receipts (llama.cpp)

A llama.cpp server that reports `timings.reasoning_budget_exhausted` gets
each step's force-close and reasoning token count stored in the session's
stream receipts. `python scripts/reasoning_budget_report.py` prints the
force-close rate per session. A step without the field counts as unknown,
not as finished.
