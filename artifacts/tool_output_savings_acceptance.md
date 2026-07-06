# Tool-output savings ledger — acceptance notes

## Scope
OMP-inspired (snapcompact-style) **measurement** for tokens avoided when Marionette compacts or truncates oversized tool outputs before they enter pilot context. This is distinct from prompt-cache savings (`cache_savings_usd`) and from LLM history compaction (`/api/session/compact`).

## Behavior
- **Deterministic math:** `tokens_saved = (original_chars // 4) - (compact_chars // 4)` using the same chars→tokens heuristic as the context meter.
- **Storage:** SQLite WAL database at `{state_dir}/tool_output_savings.sqlite`, deduped by `(session_id, tool_call_id)` via `INSERT OR IGNORE`.
- **Optional audit mirror:** set `HARNESS_TOOL_OUTPUT_SAVINGS_JSONL=1` to append `{state_dir}/tool_output_savings.jsonl` on successful inserts.
- **Hot path:** `try_record()` and compaction callbacks swallow all errors; failed writes never block tool execution.
- **Hooks:** `maybe_persist_result` and `enforce_turn_budget` in `harness/context_budget.py`, wired from `ConversationalSession._append_action_result`.

## API surfaces
| Endpoint | New session fields |
|----------|-------------------|
| `GET /api/usage` | `tool_output_tokens_saved`, `tool_output_savings_usd`, `tool_output_compactions` |
| `GET /api/swarm/live` | same |
| `GET /api/context/usage` | same |

USD uses the active driver input price (`price_in` / `resolve_price`).

## UI surfaces
- **StatusBar:** “N compacted” chip when savings > 0; CostBreakdown popover shows compact savings row.
- **SwarmPane footer:** “Compact: N” when session has recorded compactions.

## Tests run
```bash
python -m pytest tests/test_tool_output_savings.py -q
python -m pytest tests/test_usage.py tests/test_context_budget.py -q
```

## Manual smoke (optional)
1. Run harness with a session that triggers a large tool result (e.g. big `read_file` or command output).
2. Confirm `{state_dir}/tool_output_savings.sqlite` grows.
3. Poll `/api/usage` — `tool_output_tokens_saved` > 0.
4. StatusBar cost popover shows “Compact tool outputs saved”.

## Not in scope (follow-ups)
- History compaction journal (LLM summarize / snapcompact PNG strategy).
- Per-job swarm worker compaction attribution.
- Org-level rollup export.
