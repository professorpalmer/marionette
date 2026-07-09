# Next wave: Marionette multi-session / Hermes-grade workspace UX

**Status:** OPEN — deferred after Marionette **v0.9.17** and Puppetmaster **v1.13.0** (both shipped; PM on PyPI as `puppetmaster-ai==1.13.0`).  
**Audience:** fresh agent session with a clean context window.  
**Handoff path:** `docs/NEXT_WAVE_MULTI_SESSION.md` (this file) on Marionette `main` (`bff3617+`).  
**Companion thrift wave:** `docs/NEXT_WAVE_WORKER_THRIFT.md` (Puppetmaster worker compaction/offload) — orthogonal; can ship in parallel.  
**Goal:** Cursor/Hermes-grade: instant workspace swap, sessions always listed, agents keep running in background, zero ghost resumes, no blink/reload.

This doc is the handoff. Do not re-audit from scratch — the findings below are already file:line verified.

---

## What already shipped (do not redo)

### Marionette v0.9.17 (`b8f68a2`, tag `v0.9.17`)

| Fix | What it did |
|-----|-------------|
| **Explicit resume latch** | `/api/session/state` `resume_pending` is no longer “idle + trailing user turn”. Only the self-edit restart path sets a one-shot latch. Kills spontaneous agent runs when opening past sessions. |
| **Boot cost meters** | `_rebuild_pilot_and_session` / `_swap_pilot` copy token/cost meters onto the new pilot. StatusBar keeps last-good on zeros. |
| **Eager per-root session lists** | `GET /api/sessions?repo=` without switching workspace; LeftRail prefetches every rail root into `sessions:${path}`; `pathNormalize` for Windows slash/case; stale-payload guard on `onSessionsLoaded`. |
| **SSRF / Electron** | CGNAT `100.64.0.0/10` blocked; MCP HTTP `SafeRedirectHandler`; `will-attach-webview` locks prefs. |
| **Platform lock (Marionette half)** | `_init_platform_lock` treats a well-formed `disabled` list as configured even without `harness_initialized`. |

### Puppetmaster v1.13.0 (platform lock root cause + cache lifts) — SHIPPED

Tag `v1.13.0` on `60810db`; GitHub release; PyPI [`puppetmaster-ai 1.13.0`](https://pypi.org/project/puppetmaster-ai/1.13.0/).

| Fix | What it did |
|-----|-------------|
| **`platform_lock._write_disabled` RMW** | Preserves foreign keys (`harness_initialized`) when writing `platform.json`. This was the root cause of Marionette re-disabling adapters after `platform enable`. |
| **Anthropic `cache_control`** | System + tools + rolling history breakpoints; `PUPPETMASTER_PROMPT_CACHE=0` kill switch. |
| **Static-first prompts** | Boilerplate first, task instruction last — shared cacheable prefix across swarm workers. |

### Explicitly out of scope for *this* wave (already decided)

- Full multi-session was **intentionally deferred** from v0.9.17.
- git-bridge CRLF “fix” was **disproven** (Node does not translate newlines).
- Token-as-query-param removal blocked (SSE `EventSource` needs it).
- Rate limiting on loopback+token: low value.

---

## The problem (user-visible)

From daily use (Windows primary; not a Mac regression of previously-working multi-session):

1. **Dir switch stops running agents.** Click another project → blink/reload → in-flight turn dies or is orphaned.
2. **Cannot run two sessions at once.** Cursor/Hermes: agent on repo A while chatting on repo B; swap is instant. Marionette: one global pilot.
3. **Ghost resume** — largely fixed in v0.9.17; keep regression tests green while building multi-session (do not reintroduce auto-resume on attach).
4. **Session list / flicker** — largely fixed in v0.9.17; multi-session must not regress eager lists or path normalization.

**Regression verdict (audit swarm `job_042725c8448e`):** stop-on-switch and single-active-pilot are **latent architecture**, pre-v0.9.0. Same-day UI work (workspace-scoped sessions + SWR crossfade) made flicker louder; Windows path case/slash amplified empty lists. Not “Windows broke Mac.”

---

## Reference architectures (study locally / GitHub)

### Hermes (`C:\Users\pwall\hermes-agent`) — primary model to copy

- **`_sessions: dict[sid, dict]`** live runtime registry (separate from durable SQLite `session_id`).
- **Detached transport:** WS disconnect → `_detached_ws_transport`; agent keeps running; reconnect via `session.resume` rebinds transport.
- **Cold resume:** return transcript + `running: False` / `idle`; **deferred agent build** (`_schedule_agent_build`); never auto-`prompt` on open.
- **Project scope is view-only** (`$projectScope`) — does not kill agents.
- **Desktop cache:** `sessionStateByRuntimeIdRef` / warm-cache fast path for instant swap.
- **Leases:** `max_concurrent_sessions` in `hermes_cli/active_sessions.py`.

Key files: `tui_gateway/server.py`, `gateway/session.py`, `apps/desktop/src/app/session/hooks/use-session-state-cache.ts`, `apps/desktop/src/store/projects.ts`.

### Oh My Pi ([can1357/oh-my-pi](https://github.com/can1357/oh-my-pi)) — secondary (listing / safe switch)

- **Single `AgentSession`** — abort + `switchSession` + `replaceMessages` for display; **no** parallel chats in one process.
- **Cheap listing:** prefix/tail JSONL reads (`session-listing.ts`) — titles without full hydrate.
- Copy for polish; **do not** copy single-agent abort-on-switch as the concurrency model.

Local clone may be missing (`C:\Users\pwall\oh-my-pi` was absent); clone if needed.

---

## Current Marionette architecture (what must change)

**Single-active assumptions (seam inventory):**

| Seam | Location (approx) | Problem |
|------|-------------------|---------|
| Global `_pilot` / `_session` | `harness/server.py` ~1085–1103 | One `ConversationalSession` for the process |
| `_rebuild_pilot_and_session` | `server.py` ~1195–1230 | Workspace open replaces live pilot; meters now copied, execution still torn down |
| `/api/workspace/open` | `server.py` ~2063–2148 | No `_busy` guard (unlike session switch 409); force-switches to newest session in target repo |
| `/api/sessions/switch` | busy lock | Refuses if busy — does not background |
| SSE disconnect | chat/auto streams | `gen.close()` / `_pilot.cancel()` on client drop — **view detach cancels turn** |
| `SessionStore._active` | `harness/sessions.py` | One active pointer; promotion on delete is workspace-scoped (keep that invariant) |
| Frontend `activeSessionId` | `App.tsx`, `Conversation.tsx` | Remount/reload transcript on switch; stream cancel on navigate |
| Usage meters | `/api/usage` | Still pilot-object-scoped (survives rebuild now); multi-pilot needs process-level or per-session aggregation |

**State scoping invariants (must preserve — `.cursor/rules/state-scoping.mdc`):**

1. Never persist temp-dir-rooted state as boot-restorable.
2. Active-session promotion stays inside the **same workspace root**.
3. Job reads/actions resolve harness store **and** CLI durable store.
4. Dead orchestrator jobs must not show as running.
5. Session metadata in `~/.pmharness/state/harness_sessions.json`; transcripts in `state/transcripts/<sid>.json`.

---

## Target design (pragmatic blend)

### Phase A — Non-cancelling view switch (smallest concurrency win)

1. **SSE detach ≠ cancel.** Closing EventSource / navigating away must **not** call `_pilot.cancel()` unless the user hits Stop. Buffer or drop events for detached UI; reattach on return.
2. **Busy workspace open:** if current pilot is busy, either (a) 409 with clear message, or (b) leave old runner alive and only change the **view pointer** (preferred once Phase B exists).
3. Keep ghost-resume latch; add regression: opening session with trailing user message must **not** call `api.resume`.

### Phase B — Per-session runner registry (Hermes-shaped)

1. Replace singleton `_pilot` with `dict[session_id, ConversationalSession]` (or `SessionRunner`), capped by a lease (`max_concurrent_sessions`, default small e.g. 2–4).
2. **Active view** = which session’s stream/transcript the UI attaches to; **not** which runners may execute.
3. Workspace switch = change view + ensure a runner exists for the target session; **do not** destroy other runners.
4. Rail: per-session `running` / `idle` badge from runner state.
5. Frontend: cache transcripts per session id; warm swap without destructive remount (mirror Hermes state cache).
6. `/api/usage`: process-lifetime boot meters (already intended) + optional per-session breakdown later.

### Phase C — Deferred agent construction / display vs model history

1. Cold open: load transcript, return idle, schedule agent/warmup off the response path (Hermes `_schedule_agent_build`).
2. Sanitize model history separately from display transcript (Hermes `sanitize_replay_history` / OMP `transcript: true`) so interrupted tool tails don’t stick the UI.

### Phase D — Polish

1. Optimistic session row on first send (Hermes `upsertOptimisticSession`).
2. Ring-buffer / event buffer for detached mid-turn reattach.
3. Expand-without-activate already partially done — verify chevron never calls `handleOpenProject`.

---

## Ranked implementation order (ship slices)

1. **SSE detach without cancel** + regression tests for ghost resume (trust + enables background work).
2. **Runner map keyed by session id** with lease cap; workspace open becomes view change when possible.
3. **Frontend warm cache + running badges.**
4. **Deferred build / history sanitization.**
5. **Polish** (optimistic rows, ring buffer).

Do **not** attempt 1–5 in one implement worker. Use **disjoint file scopes** across parallel workers (lesson from v0.9.16: worker patch apply can roll back uncommitted live-tree edits). Prefer commit between workers.

---

## Suggested worker split (collision-safe)

| Worker | Owns | Must not touch |
|--------|------|----------------|
| A — detach/cancel semantics | `harness/server.py` stream handlers, cancel paths; tests for disconnect | `webapp/src/components/LeftRail.tsx` |
| B — runner registry | `server.py` pilot lifecycle, `conversation.py` busy/lease; new small module e.g. `harness/session_runners.py` | Electron, url_safety |
| C — frontend attach | `Conversation.tsx`, `App.tsx`, transport; vitest | `harness/url_safety.py` |
| D — rail badges / SWR | `LeftRail.tsx`, api types | stream cancel logic |

Run security/SSRF and platform-lock work is **done** — leave those files alone unless a regression appears.

---

## Tests that must exist / stay green

- Trailing user turn + idle → `resume_pending` **false** (v0.9.17).
- Self-dev restart latch → `resume_pending` **true** then clears (`tests/test_self_dev_restart.py`).
- Meters survive `_rebuild_pilot_and_session` (`tests/test_usage_meters_survive_rebuild.py`).
- `/api/sessions?repo=` does not change active workspace.
- **New:** UI stream close does not cancel an in-flight turn (Phase A).
- **New:** two sessions can be “running” under a lease while only one is displayed (Phase B).
- **New:** workspace switch while busy does not orphan without a defined policy (409 or background).

Windows CI: avoid tight timing asserts (`test_parallel_reads` flake pattern — win32 bound already loosened).

---

## Release discipline

- Marionette: push main → wait `tests` green (3.9 + 3.11 + frontend) → **then** tag. Never tag first.
- Puppetmaster: push → CI green → tag + `gh release` + **manual twine** to PyPI (`puppetmaster-ai`). No release workflow automation.
- After multi-session ships: bump Marionette (likely v0.9.18 or v0.10.0 if architecture is user-visible enough); pin/note PM version if needed.
- Wiki ingest session decisions when done (`wiki-ingest-local` skill).

---

## Context pointers

| Item | Where |
|------|--------|
| Marionette repo | `C:\Users\pwall\Projects\marionette` |
| Puppetmaster repo | `C:\Users\pwall\Projects\Puppetmaster` |
| Hermes reference | `C:\Users\pwall\hermes-agent` |
| State scoping rules | `marionette/.cursor/rules/state-scoping.mdc` |
| UX audit swarm | `job_042725c8448e` (55 artifacts) |
| Hermes/OMP study agent | transcript subagent `6149a776-9256-463b-b676-cf911fbc53c9` |
| Prior chat transcript | `bf62d89f-c126-48a4-89c0-ceb3a63c98ad` |
| Wiki raw for v0.9.17 | `my-portable-llm-wiki/raw/conversations/2026-07-08-marionette-v0917-session-ux-ssrf-hardening.md` |

---

## Success criteria (done when)

- [ ] Agent running on project A; switch to project B and chat; A still running; swap back shows live progress without restart.
- [ ] Opening a past session never starts the agent without an explicit user send / Stop-cleared latch / self-dev restart.
- [ ] Session lists under every rail directory without click-to-activate; no stale cross-dir attach.
- [ ] Boot spend/saved cluster never blanks on workspace switch.
- [ ] Platform lock: `puppetmaster platform enable cursor` + Marionette reboot leaves cursor enabled (both sides shipped; verify end-to-end after PM 1.13.0 on PyPI).
- [ ] Full pytest + vitest green; CI green before tag.

---

## One-line brief for the next session

> Implement Hermes-style multi-session in Marionette: per-session runners + non-cancelling SSE detach + view-only workspace switch; preserve v0.9.17 resume latch / meters / eager lists and state-scoping invariants; ship in phased workers with disjoint files; do not re-litigate platform-lock or SSRF.
