# Chat-quality diagnosis (Ox Alpha / Muse Spark 1.2 inconsistency)

Diagnostic pass, read-only, over the model-routing + provider + pilot-loop layer.
Reference transcript to reproduce: `fe7ecaf24bbd`.

Run by a concurrent two-role Puppetmaster swarm (codex / gpt-5.4-mini), 2026-08-23.
Verdict: **inconsistency is harness/backend-caused, not model-caused.** The
evidence points upstream to model routing and prompt construction; the SSE
streaming layer preserves order and cursor semantics and is NOT the primary bug.

## Top root causes (ranked by expected impact on chat-surface quality)

### 1. Bare-model pilot resolution can silently reroute the same visible spec
`harness/model_visibility.py` + `harness/auto_registry.py` + `harness/providers.py`
resolve a bare model string to a concrete backend by key availability and
provider order. If discovery is transiently stale or a key is missing, the *same*
model string can land on a different provider (or fall back to OpenRouter) across
sessions. Ox Alpha / Muse Spark lose capability metadata or hit the wrong endpoint.
Mitigation: treat `~/.puppetmaster/models.json` and the auto-registry as advisory
unless discovery is stable; validate that the selected provider actually serves the
claimed model id + capabilities before promoting it into the pilot registry.

### 2. OpenCode Go mutates the outbound request shape per model family
`harness/opencode_go.py` + `harness/opencode_common.py`. Namespace stripping,
protocol selection, `max_tokens` clamping, temperature overrides, and
reasoning-body injection vary by model family. This is a harness-side source of
model-dependent quality shifts *before* the request reaches the vendor — so the
same model can behave differently depending on which branch of the adapter runs.

### 3. The turn loop rebuilds the system prompt per iteration
`harness/conversation.py` / `harness/pilot.py`. The system prompt is rebuilt on
every non-append-only step, dynamic context blocks are appended, then the base
prompt is restored after dispatch. That defeats prefix stability and adds
prompt-cache churn / turn-to-turn jitter even when the user asks the same thing.
Hermes treats prompt-cache stability as sacred; this is the same failure mode.

### 4. (Adjacent) OpenCode Zen routes "Ox Alpha Free" through the wrong API mode
`harness/opencode_zen.py` — the free variant can be sent in a degraded request
mode, which lowers output quality independent of the model itself.

## Not the bug
- SSE / stream replay: preserves event order + cursor semantics; marked misses
  as `cursor_gap` / `generation_mismatch` rather than silently replaying a hole.
- Mid-turn reattach: on a gap it hydrates from the durable transcript sidecar.

## Transcript location
Session transcripts are JSON sidecars at `<state_dir>/transcripts/<sid>.json`,
where `<state_dir>` is the server's session-state base (`_cfg.state_dir`, or the
process temp dir when unset). For `fe7ecaf24bbd`:
`<state_dir>/transcripts/fe7ecaf24bbd.json`.
