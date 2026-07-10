# Session decisions — 2026-07-09 (v0.9.28)

## OpenRouter / OpenAI-compat explicit prompt cache

- Claude-via-OR and Qwen get explicit `cache_control` (Claude: 1h stable / 5m history; Qwen: ephemeral).
- Shared helper `pmharness/drivers/prompt_cache.py`; OpenRouter `session_id` sticky routing.
- Automatic-cache providers (OpenAI, Gemini, DeepSeek, Grok, Moonshot) unchanged — already surface `cached_tokens`.
- Kill switch `HARNESS_PROMPT_CACHE=0`; Claude TTL `HARNESS_ANTHROPIC_CACHE_TTL=5m|off`.
- Paired with Puppetmaster v1.17.0.
