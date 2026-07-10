# Session decisions — 2026-07-09 (v0.9.29)

## AGNT-style all-1h + BYOK prefix hygiene

- Claude explicit `cache_control`: all breakpoints default to `ttl:1h` (native + OpenRouter agentic). Qwen ephemeral; automatic providers unmarked.
- Strip markers before re-stamp (Anthropic 4-breakpoint cap).
- `HARNESS_APPEND_ONLY_CONTEXT=auto` now enables for cloud BYOK (OpenRouter/OpenAI/Gemini/xAI/…), not only local KV hosts — CG/wiki/budget go to the user trailer.
- Advisory history compaction runs once per user turn, not every tool-loop step; `force=True` on CONTEXT_OVERFLOW kept.
- Paired with Puppetmaster v1.18.0. No live OpenRouter re-bench this release (cost hold).
