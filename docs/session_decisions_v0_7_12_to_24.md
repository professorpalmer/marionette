# Marionette Harness -- Decisions & Fixes (v0.7.12 -> v0.7.24)

Continuation of docs/session_decisions_v0_7_x.md. Same conventions: source-run
distribution, no binary; backend edits go live on restart; UI edits live via Vite
HMR. Puppetmaster ships as PyPI `puppetmaster-ai` (fix worker/adapter behavior in
Marionette's bridge, never upstream, to avoid onboarding friction).

## Token economics (deepened -- the core competitive story)
- Cache-read discount in COST: `CACHE_READ_MULTIPLIER = 0.1` in harness/server.py
  (`_session_cost`/`_cache_savings`/`_job_cost`). Cached prompt tokens were billed
  at full input price; now billed at ~10%. `/api/usage` surfaces `tokens_cached` +
  `cache_savings_usd`; StatusBar shows the dollars saved.
- Real-usage context meter: harness/conversation.py tracks `_last_prompt_tokens`
  from actual driver usage; `_estimate_context_tokens` returns max(real, chars//4)
  so the compaction trigger + composer % track billed context, never under-count.
- Job cost uses the real input/output split, not a 50/50 blend.
- Two-breakpoint Anthropic cache (pmharness/drivers/anthropic.py): system +
  last-tool + a STABLE second-to-last-message marker (reused as a cache READ next
  turn) + a MOVING last-message marker (extends the prefix). Within Anthropic's
  4-marker limit. Keeps multi-turn input cost near-flat as the conversation grows.
- Worker token attribution: a "no changes produced" worker used to record tokens=0
  (no cost shown in the tracker); now reads real res.tokens_in/out. The success
  path also now adds _tokens_out to the session meter (was undercounting output).
- A big "N cached (~$X saved)" with tiny live cost is CORRECT and expected on long
  sessions; it is cumulative per backend process and resets on restart (by design).

## Positioning: aligned incentives (README thesis)
- A model-lab tool sells tokens, so it optimizes caching only to the "competitive"
  point (cached reads billed ~10%, not free; savings rarely surfaced). An
  independent kernel makes nothing from token count, so its incentives are purely
  to minimize spend and prove it: send less (CodeGraph retrieval), re-send cheap
  (caching), bill honestly (real discount, visible savings), and route to the
  cheapest sufficient model including a competitor's open-weights. That routing-off-
  our-own-tokens alignment is something a first-party tool structurally cannot match.

## Security
- Centralized do_GET auth gate (harness/server.py): ~11 GET endpoints (/api/memory,
  /api/config, /api/skills, /api/rules, /api/commands, /api/settings, /api/platform,
  /api/jobs, /api/workspace, /api/mcp*) were UNAUTHENTICATED. Single gate at the top
  of do_GET now authenticates every non-public path; public allowlist is only the
  renderer bootstrap assets. serve()'s reuse probe was fixed to treat any HTTP
  response (incl. 403) as "a server is running" so the auth gate didn't break restart.

## Swarm reliability
- infer_roles (pmharness/intent.py) broadened so common broad goals fan out to
  multiple workers; pinpoint lookups ("callers of X", "where is X") stay single
  (pinpoint precedence).
- _compact_artifact reads headline text from many payload keys incl. stdout.
- _promote_degraded_prose (pmharness/bridge.py): when a worker analyzes in PROSE
  instead of calling submit_findings, the agentic adapter parks final_text in a
  VERIFICATION artifact's stdout and marks it degraded; the digest hid it as
  plumbing, so audits read "completed without structured findings" despite real
  work. Now, when there are no signal artifacts but a verification carries
  substantial prose, promote a copy to a 'finding'. Verified live. Lives in the
  bridge on purpose (PyPI distribution); do NOT move upstream.

## Data-loss / correctness fixes
- Large-message paste (huge transcripts) silently vanished: the chat stream is an
  SSE GET (EventSource is GET-only) with the message in the URL query string; a big
  message exceeded the ~64KB HTTP request-line limit and was dropped. Fix: message
  stash -- POST /api/chat/stash stores {message,images}, returns a short id; GET
  /api/chat?mid=<id> pops it. api.ts chat()/auto() route messages > 4000 chars (or
  large image lists) through the stash. Body travels via POST, never the URL.
- Sent-message images showed broken icons: bubble rendered src=previewUrl, a blob:
  URL revoked after send / gone on reload, and there was no endpoint to serve the
  saved upload. Fix: GET /api/image?path=... serves ONLY files under _UPLOAD_DIR
  (os.path.realpath prefix check -> 403 outside; 404 missing; image extensions;
  size-guarded), authenticated. api.ts imageUrl(path) = withToken('/api/image...').
  Sent images render from api.imageUrl(img.path), falling back to previewUrl -- so
  they render after send AND after a transcript reload. NEEDS BACKEND RESTART.
- "session busy: another request is in flight" after stopping mid-tool-call:
  interrupt()/cancel() only set the _cancel flag; the in-flight generator releases
  _busy in its finally, but when stopped WHILE a subprocess/tool runs the generator
  is blocked there and never reaches finally, so _busy stays held. The normal stale-
  recovery required _state == 'idle' (still 'executing' mid-tool), so the next
  message wrongly errored busy. Fix: interrupt() sets _interrupt_requested; the
  busy-acquire path force-recovers after a 0.5s grace even while 'executing',
  cleared when a new turn cleanly acquires. Non-interrupted in-flight turns still
  reject re-entry.

## Performance (macOS window-switch stutter + focused CPU)
- Cause of alt-tab stutter: GPU compositor saturation. ~65 continuous CSS
  animations (spinners/pulses) kept the SHARED macOS compositor pinned and never
  paused when backgrounded. Fix: html.app-idle (index.css) pauses all animations;
  App.tsx toggles it on blur/focus/visibilitychange. Measured ~20x renderer CPU
  drop when blurred (~40% -> ~1.8%).
- Focused-state CPU: the streaming bubble ran full ReactMarkdown + remarkGfm +
  rehypeHighlight on every ~60fps typewriter frame. Fix: render plain text while
  streaming, parse Markdown ONCE on finalize. Measured ~40% -> ~20% focused.
- Transcript windowing: RENDER_WINDOW was 200 display groups (never fired). Lowered
  to 40 so long sessions cap and show "Show earlier messages"; big DOM + perf win.
- SwarmPane nowTick 1s -> 5s and paused while hidden.
- Setup wizard only auto-opens on genuine first-run (no key); marks wizardSeen so
  it never nags again (was popping every launch via `seen === null`).
- Markdown now renders inside the collapsible tool-call breakdown (per-step
  narration + reasoning block) -- was raw whitespace-pre-wrap. Gentle fade+slide as
  a finished streaming bubble folds into the "Investigated" box (was a jumpy snap).

## Docs
- README overhauled: Documentation index; accurate tiered vision; removed the false
  DMG/exe/AppImage download section (contradicted RELEASING.md's source-run model);
  added the aligned-incentives positioning. ARCHITECTURE gained code-grounded
  sections on HTTP auth, token economics, and persistence.

## Wiki orchestrator note
- The portable-llm-wiki backend only reaped orphaned "running" jobs at STARTUP, so
  zombie jobs lingered across days. Downstream fixes applied so a wiki backend
  restart cleanses stale jobs. (Separate repo; not a Marionette bug.)
