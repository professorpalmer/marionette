# Design: provider call record/replay (cassettes)

## Problem

The rig proves driving quality offline via `StubDriver` / `stub-oracle-v2`
(`pmharness/drivers/stub.py`, `tests/test_harness_e2e.py`, `tests/test_e2e_multiturn.py`)
but **pilot and worker LLM calls** still require live keys or hand-written stubs.
Flaky provider responses make regression tests non-deterministic. A cassette layer
would record real `Driver.complete` / `Driver.chat` exchanges once, then replay
them in CI with zero network and stable token accounting.

## Proposed shape

**Cassette file:** `{fixtures}/cassettes/{driver_name}/{cassette_id}.yaml`

```yaml
version: 1
driver: openrouter/gpt-4o-mini
recorded_at: "2026-07-06T12:00:00Z"
interactions:
  - request_hash: sha256:abc...
    method: chat
    request:
      messages: [...]          # normalized (roles, content; tool_calls sorted)
      tools: [...]             # optional; schema names only in replay mode
    response:
      text: "..."
      tokens_in: 120
      tokens_out: 45
      model: gpt-4o-mini
    scrubbed_fields: [api_key]
```

**Modes (env):**

- `HARNESS_CASSETTE_MODE=record` — wrap driver, append new interactions.
- `HARNESS_CASSETTE_MODE=replay` — match by `request_hash`; error if missing.
- unset — passthrough (current behavior).

**Request hashing:** canonical JSON of `(method, messages, tool_names, model)`
after redaction; optional `cassette_id` label on `HarnessConfig` selects file.

**Secret scrubbing:** strip `Authorization` headers and env key patterns before
write; replay injects dummy key. Never commit raw keys (same rule as
`results/*.sqlite` in AGENTS.md).

## Integration points

| Layer | Hook |
|-------|------|
| Wrapper | New `pmharness/drivers/cassette.py` — `CassetteDriver(inner: Driver)` implementing `complete` / `chat`. |
| Factory | `pmharness/drivers/__init__.py` or harness driver resolution in `harness/config.py` — when mode set, wrap resolved driver. |
| Normalization | Shared `pmharness/drivers/cassette_normalize.py` for stable message hashing (tool_call ids replaced with indices on record). |
| Pilot path | `harness/pilot.py` provider calls unchanged; wrapping happens at driver construction in session bootstrap. |
| Worker path | Same wrapper for worker LLM adapter if workers use a distinct driver seam (`harness/edit_engines.py` native edit is out of scope for v1). |
| CLI | `python -m pmharness cassette record|replay <task>` for fixture authors (optional; tests can set env only). |

## Test strategy

- Unit: record one stub interaction, replay twice, assert identical `DriverResponse`
  and hash match; missing cassette raises clear error in replay mode.
- Hermetic: `tests/test_cassette_driver.py` with tmp fixture dir; no network.
- Guard: CI job runs replay suite only; record mode excluded from default workflow.
- Existing offline E2E (`test_harness_e2e.py`) stays on stub-oracle; add one
  cassette-backed test that proves wrap does not break Session event stream.

## Non-goals (this tranche)

- HTTP-level VCR for arbitrary URLs (only driver protocol boundary).
- Cassette merging / conflict resolution UI.
- Recording tool execution or Puppetmaster worker subprocess output.
- Multi-model stochastic sampling replay (temperature > 0 requires fixed seed + stored choice).
- Encrypting cassettes at rest (scrubbing only).
