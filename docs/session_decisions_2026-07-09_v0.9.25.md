# Session decisions — 2026-07-09 (v0.9.25)

## Broaden swarm-gate for investigate / cross-platform

- Symptom: a Windows-vs-Mac "find out" turn stayed inline (30+ tools)
  even though the swarm delegate gate existed.
- Fix: expand `_BROAD_INTENT_RE` for investigate/cross-platform wording;
  lower `DELEGATE_THRESHOLD` default from 8 to 4 so borderline broad
  intents actually route to swarms.

## Truthful labels on local implement jobs

- Symptom: agentic/OAuth implement jobs showed the OpenRouter pilot slug
  and "provider worker" in the swarm tracker.
- Fix: stamp adapter/model/role from the edit engine (`implement (agentic)`,
  `agentic/z-ai/...`) instead of the pilot driver; keep-alive on FAILED
  so a ~137ms silent fail no longer looks like the user killed the pilot.

## Scope running jobs like finished ones

- Symptom: a running OAuth implement leaked into every workspace's tracker;
  finished jobs were already session/cwd scoped.
- Fix: drop the "running is always visible" bypass; apply the same
  session_id / cwd rules; orphan escape only when neither is set.

## Google OAuth webview (installer-only validation)

- Second-pass Client Hints / preload / `openExternal` escape for the
  in-app browser. Electron lives in `app.asar` — verify on
  `Marionette-0.9.25-Setup.exe`, not the git checkout.
