# Session decisions — 2026-07-09 (v0.9.23)

## Swarm tracker showed GPT though Models only enabled OpenRouter

- Symptom: pilot picker had 5 OpenRouter models; swarm tracker showed
  `openai/gpt-4-0613` / Cursor GPT and the worker failed (`http_status:400` /
  `sdk_not_installed`).
- Cause: Models toggles only curate the *pilot* picker. Agentic analysis
  swarms `auto_route` against `~/.puppetmaster/models.json` with
  `prefer_plan_billed=True`, so the router first-picked Cursor GPT ($0 plan),
  then `router-fallback` jumped to another non-OpenRouter GPT after
  `sdk_not_installed`.
- Fix: agentic swarm payloads set `allowed_adapters=["agentic"]` and
  `prefer_plan_billed=False` so first pick and fallback stay on the agentic
  OpenRouter set that `auto_registry` mirrors from Models toggles.
