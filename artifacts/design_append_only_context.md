# Design: append-only context mode (Marionette Round 11)

> Status: OPT-IN VIA AUTO-DETECTION (v1). Freezes the system prompt prefix for
> local/cache-discounting providers; no provider-native cache APIs, no compaction
> behavior changes, no frontend work this round.

## Defect

Local model servers (Ollama, LM Studio, llama.cpp, vLLM, sglang) and
cache-discounting providers (DeepSeek-style) reuse prefix KV cache when the
leading bytes of a request match the previous request. Marionette destroys that
reuse every step: in `harness/conversation.py` the send loop rebuilds the system
prompt each attempt — `sys_prompt = base_sys` plus the per-turn CodeGraph
section, the MCP tools section, and the turn-budget note — and assigns it into
`self._history[0]["content"]`. Because message zero changes whenever the user
message changes (CodeGraph slice is keyed to `user_message`), the prefix diverges
at byte one and the whole conversation re-prefills every turn.

## Contract

When append-only context mode is active:

1. **Frozen prefix:** `base_sys + mcp_section` is computed once (first step of
   the first turn after session start or after compaction) and stored. The system
   message in `_history[0]` is never rewritten on later steps or turns.
2. **Dynamic content in the latest user message:** The CodeGraph section and the
   turn-budget note are appended under a clearly marked trailer
   (`\n\n[context for this turn]\n` + sections) on the current turn's user
   message before it enters `_history`, so the rendered transcript grows by
   append-only suffixes while the leading bytes stay stable.
3. **Compaction is an accepted cache miss:** When `_maybe_compact_history`
   rewrites history, the frozen prefix is cleared and recomputed once from the
   new baseline. One cache miss per compaction — already amortized by the
   compaction budget.

**MCP catalog note:** The MCP tools section is fixed at freeze time. Later MCP
catalog changes do not rewrite the prefix in v1.

## Auto-detection (Task B)

`HARNESS_APPEND_ONLY_CONTEXT` values: `auto` (default), `on`, `off` (also
accepts `1`/`0`/`true`/`false`; garbage falls back to `auto`).

| Setting | Behavior |
|---------|----------|
| `on` | Always enable |
| `off` | Never enable |
| `auto` | Detect from driver name and base URL |

Auto-detection enables when **either**:

- Driver/provider name (case-insensitive substring) contains any of:
  `ollama`, `lm-studio`, `lmstudio`, `llama.cpp`, `llamacpp`, `vllm`, `sglang`,
  `deepseek`
- Base URL hostname is loopback (`localhost`, `127.0.0.1`, `0.0.0.0`, `::1`,
  `[::1]`), RFC1918 (`10.*`, `192.168.*`, `172.16-31.*`), or ends in `.local`

Unparseable URLs do not match. Resolution is a pure function; never raises.

## Non-goals

- Provider-native prompt-caching APIs (Anthropic `cache_control`, OpenAI prompt
  caching headers) — a later round.
- Changing compaction triggers or history schema.
- Reordering existing history records.
- Frontend / webapp changes.

## Visibility (Task D)

Usage payload (`get_context_usage`) exposes:

- `append_only_context` (bool) — mode active for this session
- `prefix_stable_turns` (int) — count of consecutive model calls where the
  rendered prompt started with the previous call's full prompt; resets on
  compaction or when mode is off

Best-effort; never raises.
