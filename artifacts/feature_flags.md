# Marionette feature flags (`HARNESS_*`)

Authoritative reference for every `HARNESS_*` environment variable read from
`harness/*.py`. Values are case-insensitive unless noted. Toggle flags accept
`1`/`true`/`yes`/`on` for enable and `0`/`false`/`no`/`off` for disable unless
a row states otherwise.

| Flag | Default | Effect | Introduced-in |
| --- | --- | --- | --- |
| `HARNESS_ADVISOR` | off | Opt-in second-pass advisor warnings after tool actions (`harness/advisor.py`). | Round 6 / 0.7.46 |
| `HARNESS_ADVISOR_COMPACTION` | off | When on, lowers history-compaction trigger from 75% to 65% of context budget when compaction advice level is `now`. | Round 8 / 0.7.48 |
| `HARNESS_ADVISOR_NOW_TOKENS` | `270000` | Absolute hot-now token floor for compaction advice on large windows; `min(ratio, tokens/budget)` binding rule. Set `0` to disable absolute rule. | Round 10 / 0.8.0 |
| `HARNESS_ADVISOR_SOON_TOKENS` | `150000` | Absolute hot-soon token floor for compaction advice on large windows; `min(ratio, tokens/budget)` binding rule. Set `0` to disable absolute rule. | Round 10 / 0.8.0 |
| `HARNESS_ALLOW_PRIVATE_URLS` | off | Allows HTTP fetches to private/loopback hosts in `url_safety.py` (normally blocked). | - |
| `HARNESS_APPEND_ONLY_CONTEXT` | `auto` | Append-only context mode: `on`/`off`/`auto`. When active, freezes the system prompt prefix and moves per-turn CodeGraph and turn-budget content into the latest user message for KV-cache reuse on local/discount providers. Auto-detects Ollama, LM Studio, llama.cpp, vLLM, sglang, DeepSeek, and loopback/RFC1918/`.local` base URLs. | Round 11 / 0.9.0 |
| `HARNESS_AST_PREVIEW` | off | Attaches structural AST diff metadata to `hash_edit` tool results. | Round 6 / 0.7.46 |
| `HARNESS_AUTO_COMMAND_GUARD` | on | Blocks risky shell patterns in auto-run commands; set `off` to disable (not recommended). | - |
| `HARNESS_AUTO_DISTILL` | off | Auto-runs skill distillation after pilot turns when enabled. | - |
| `HARNESS_AUTO_KILLSWITCH` | empty | Path watched by autobudget; touching the file stops autonomous runs. | - |
| `HARNESS_AUTO_MAX_IDLE` | `3` | Autobudget: max idle steps before stop. | - |
| `HARNESS_AUTO_MAX_SECONDS` | `3600` | Autobudget: wall-clock cap for autonomous runs. | - |
| `HARNESS_AUTO_MAX_SWARMS` | `40` | Autobudget: max swarm launches per autonomous session. | - |
| `HARNESS_AUTO_MAX_TOKENS` | `100000` | Autobudget: token budget for autonomous runs. | - |
| `HARNESS_AUTO_VERIFY` | on | Runs inferred verify command after edits (`config.py` default `true`). | - |
| `HARNESS_AUTO_VERIFY_MAX` | `2` | Max automatic verify passes per user message. | - |
| `HARNESS_AUTO_VERIFY_TIMEOUT` | `30` | Seconds before an auto-verify subprocess is killed. | - |
| `HARNESS_BROWSER_ENABLED` | on | Exposes browser automation tools when true (`config.py` default). | - |
| `HARNESS_BUDGET` | `3` | Default provider budget tier for routing. | - |
| `HARNESS_COMMAND_TIMEOUT` | `120` (UI) / unbounded if empty | Shell command timeout in seconds; `0`/`none`/`off`/empty disables the cap in `command_policy.py`. | - |
| `HARNESS_COMPACTION_ADVISOR` | on | Computes and surfaces layer-pressure compaction advice on usage API; set off to skip. | Round 8 / 0.7.48 |
| `HARNESS_CONFIG` | `~/.harness.json` | Path to JSON config file loaded by `HarnessConfig.from_env()`. | - |
| `HARNESS_DECLARATIVE_CHECKS` | auto | When not explicitly off, enables declarative checks if `.marionette/checks` or session `checks/` exists. | Round 4 / 0.7.45 |
| `HARNESS_DRIVER` | from config | LLM driver id (e.g. `qwen3-coder-30b`, `stub-oracle-v2`). | - |
| `HARNESS_EDIT_ENGINE` | auto | Overrides edit-engine selection (`agentic`, `native`, etc.). | - |
| `HARNESS_EVAL_HISTORY` | on | Persists declarative check outcomes to SQLite; set `0` to disable. | Round 6 / 0.7.46 |
| `HARNESS_HASH_EDIT` | off | Enables the `hash_edit` tool in the visible catalog. | - |
| `HARNESS_IMPLEMENT_DEEP` | off | Allows higher-capability implement models when set. | - |
| `HARNESS_IMPLEMENT_MAX_CAPABILITY` | `86` | Capability floor for deep implement routing. | - |
| `HARNESS_IMPLEMENT_MODEL` | empty | Explicit model id for agentic implement actions. | - |
| `HARNESS_IMPLEMENT_PROVIDER` | empty | Provider override for agentic implement (`openrouter`, etc.). | - |
| `HARNESS_KEY_ENV` | `{REACH}_API_KEY` | Env var name used to load provider API keys. | - |
| `HARNESS_KEY_FILE` | empty | File path whose contents are loaded as the live API key at server start. | - |
| `HARNESS_MAX_CONTEXT_TOKENS` | from driver | Hard cap on conversation context window size. | - |
| `HARNESS_MAX_PILOT_STEPS` | `40` | Safety cap on pilot/swarm round-trips per user message (`0` = unlimited in UI). | - |
| `HARNESS_MAX_TOKENS` | `8000` | Default max output tokens for provider calls. | - |
| `HARNESS_MAX_TOOL_RESULT_CHARS` | `24000` (conversation) / `8000` (context_budget) | Truncation threshold for tool output retained in history. | - |
| `HARNESS_MAX_WORKERS` | `64` | Upper bound on concurrent worker slots in the server. | - |
| `HARNESS_NO_DELEGATION` | off | Hides delegation tools from the visible catalog for leaf workers. | - |
| `HARNESS_OFFLOAD_MARGIN` | `0.9` | Maximum replacement/original char ratio for tool-output offload; above this the result stays verbatim. | Round 10 / 0.8.0 |
| `HARNESS_OFFLOAD_MIN_TOKENS` | `3000` | Minimum estimated tokens before tool-output spill/compaction is considered. | Round 10 / 0.8.0 |
| `HARNESS_REACH` | `openrouter` | Default provider reach (`openrouter`, etc.). | - |
| `HARNESS_REPO` | empty | Target git repository path for real swarm/analysis runs. | - |
| `HARNESS_REVIEW_EDITS_BEFORE_APPLY` | off | Requires human approval before applying batched file edits. | - |
| `HARNESS_SHOW_CONSOLES` | off | Windows only: disable hidden-console subprocess wrapping when `1`. | - |
| `HARNESS_SPILL_RETENTION_DAYS` | off (`0`/empty) | When set to a positive integer, counts spill rows older than N days in L3 layer metrics; does not auto-delete. | Round 7 / 0.7.47 |
| `HARNESS_STATE_DIR` | `~/.pmharness/state` | Session state root (SQLite, journals, keys, spill index). | - |
| `HARNESS_SWARM_ADAPTER` | `demo` or `agentic` | Puppetmaster adapter for swarm workers (`demo` when repo empty). | - |
| `HARNESS_TOKEN` | random | Bearer token for local HTTP API auth; auto-generated if unset. | - |
| `HARNESS_TOOL_DISCOVERY` | on (`1`) | Enables dynamic `search_tools` catalog activation; set `0` to disable. | - |
| `HARNESS_TOOL_OUTPUT_SAVINGS_JSONL` | off | Mirrors tool-output savings ledger rows to JSONL for audit. | - |
| `HARNESS_TURN_BUDGET` | on | Parses `+Nk`/`+Nk!` output-budget directives from user messages; advisory note in system prompt, hard ceiling stops the tool loop. | Round 10 / 0.8.0 |
| `HARNESS_TURN_BUDGET_CHARS` | `48000` | Soft char budget consulted by context-budget helpers. | - |
| `HARNESS_TURN_CONTEXT` | on | Journals per-turn flag fingerprints to `turn_context.jsonl`; set `0` to disable. | Round 6 / 0.7.46 |
| `HARNESS_TURN_DEADLINE_SECONDS` | `600` | Wall-clock deadline for a single conversational turn. | - |
| `HARNESS_UPLOAD_MAX_BYTES` | `10485760` (10 MiB) | Max upload size for file-ingest endpoints. | - |
| `HARNESS_VERIFY_CMD` | empty | Legacy verify command alias (see `HARNESS_VERIFY_COMMAND`). | - |
| `HARNESS_VERIFY_COMMAND` | empty | Explicit verify shell command run after edits. | - |
| `HARNESS_VERIFY_MAX_RETRIES` | `2` | Retries for the post-edit verify hook. | - |
| `HARNESS_VERIFY_TIMEOUT` | `180` | Seconds before verify subprocess is killed. | - |
| `HARNESS_VLM_MODEL` | provider default | Overrides vision-language model id for image inputs. | - |
| `HARNESS_VLM_REACH` | auto | Forces OpenRouter (or other) reach for VLM calls when set. | - |
| `HARNESS_WIKI_AUTO` | off | Auto-ingests session digest to portable-llm-wiki when URL+token configured. | - |
| `HARNESS_WIKI_ORCHESTRATE` | off | When `auto`, ingests wiki pages immediately on orchestration events. | - |
| `HARNESS_WIKI_SUBDIR` | `conversations` | Subdirectory under wiki `raw/` for ingested sessions. | - |
| `HARNESS_WIKI_TOKEN` | empty | Bearer token for wiki ingest API. | - |
| `HARNESS_WIKI_URL` | empty | Base URL of the portable-llm-wiki backend. | - |
| `HARNESS_WORKER_DEADLINE_SECONDS` | `900` | Wall-clock deadline for a single worker run. | - |

## Notes

- `HARNESS_MAX_TOOL_RESULT_CHARS` defaults differ between `conversation.py` (24000) and `context_budget.py` (8000); callers pick their own fallback — not a single global default.
- `HARNESS_DECLARATIVE_CHECKS` design notes mention YAML specs; runtime only loads JSON check files (`declarative_checks.py`).
- `PMHARNESS_*` variables (`PMHARNESS_PYTHON`, `PMHARNESS_LIVE_MODELS`, `PMHARNESS_MODELS_CACHE_TTL`, `PMHARNESS_MCP_ALLOW_PRIVATE`, `PMHARNESS_EVENT`) are related but outside the `HARNESS_*` prefix and are not listed above.
