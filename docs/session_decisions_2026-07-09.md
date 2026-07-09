# Session decisions — 2026-07-09 (v0.9.19)

## Memory propose (interactive only)

- Agent `memory add` queues candidates; nothing persists mid-turn.
- After `assistant_done`, emit non-blocking Save/Skip cards (max 3, exact-text dedupe).
- Autopilot (`_auto_mode`): no propose, no persist from this path.
- Manual Settings add stays direct (`source: user`).
- Full OMP/Mnemopi end-of-turn consolidation deferred (backlog).

## State pane warm polish

- Keep primary StatePane mounted (CSS-hidden) across right-rail tab swaps.
- `GET /api/wiki/status` returns counts only; State strip stops fetching full graph.

## Multi-session / UX follow-ups in this cut

- Per-runner config so mutating `_cfg.repo` does not retarget busy pilots.
- Process-lifetime spend tally across dirs/sessions.
- Stable PROJECTS order; empty-project New session CTA.
- Remove duplicate top Tasks bar (`TaskStack`).
- Swarm ROUTING display dedupe; branch switch dirty confirm + toast.
- Remove stale status-bar "N jobs" badge (LeftRail SESSION JOBS is source of truth).
