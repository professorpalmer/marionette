# Session decisions — 2026-07-09 (v0.9.22)

## Pilot default stuck on glm-5.2 (Windows)

- Symptom: Settings DRIVER saved as deepseek-v4-flash (or any choice) but the
  chat picker snapped back to `z-ai/glm-5.2` on relaunch.
- Cause: Saves wrote `~/.pmharness/state/workspace_drivers.json`, but boot
  restored drivers *before* anchoring `HARNESS_STATE_DIR` to that `state/`
  dir, so it read the missing legacy path and fell through to
  `enabled_pilots()[0]` (user's Models toggle order starts with glm-5.2).
- Fix: Anchor durable state first; prefer `state/` for reads with a safe
  legacy fallback; never nest `state/state`. Models toggles remain picker
  curation only — default is Settings DRIVER / PilotPicker.

## Wiki stats readability

- Inline bold `792 pages, 8084 links` was hard to read at some resolutions.
- Match CodeGraph: label-over-value grid (Pages / Links), muted semibold
  numbers, URL on its own line.

## Also in this cut

- Collapsible Models provider groups (OpenRouter starts collapsed).
- Cross-project session cache purge on delete (phantom / "merged dir" ghosts).
- GPT-5.6 Sol/Terra/Luna in OpenAI / OpenRouter curated pilot lists.
