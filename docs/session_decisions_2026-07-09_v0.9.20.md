# Session decisions — 2026-07-09 (v0.9.20)

## Swarm tracker truth + heat

- Model badge and cost follow **final** routing (`router-fallback` / escalation), not the initial plan-billed Cursor pick that failed over to agentic.
- Dead-run detection stays honest when live payloads are slim: server stamps `dead_run_failure` before stripping findings.
- `/api/swarm/live` ships **slim** artifacts for terminal jobs (ROUTING + verdicts only); expand lazy-loads `/api/artifacts`. Running jobs stay full-stream.
- Poll merge keeps hydrated full artifacts; `swarm:*` SWR keys soft-persist in sessionStorage for remount warmth.
- Findings section remains collapsible.

## Session switch empty flash

- Transcript cache miss keeps stale rows (dim + block send) instead of clearing to "Message the pilot".
- LeftRail only pushes real session ids on project load (no empty `onSessionChange("")`).

## Wiki auto-grounding (QoL ledger)

- Pilot turns can inject a short wiki grounding section (search + trailer / sys_prompt paths).
- Local savings ledger in `harness/wiki_grounding_savings.py` for auditability — not a marketing claim.

## Companion Puppetmaster

- Ships with Puppetmaster **v1.14.1** pricing/usage fix (final routing + best usage record). Marionette does not pin a PyPI floor in pyproject; dogfood uses editable / installed site-packages.
