# Provider cassettes — acceptance notes

## Scope

Record/replay layer for provider driver calls at the pmharness Driver protocol
boundary. JSON cassettes (not YAML) under ``HARNESS_CASSETTE_DIR``.

## Behavior

- **Wrapper:** ``pmharness/drivers/cassette.py`` — ``CassetteDriver`` /
  ``maybe_wrap_cassette``.
- **Modes:** ``HARNESS_CASSETTE_MODE=record|replay``; unset = passthrough.
  ``HARNESS_CASSETTE_DIR`` required when mode is set.
- **File:** ``{HARNESS_CASSETTE_DIR}/{sanitized_driver_name}.json`` with
  ``version``, ``driver``, ``recorded_at``, ``interactions[]``.
- **Hashing:** canonical JSON of ``(method, model, normalized_messages,
  tool_names)``; tool call ids replaced by ordinal index.
- **Scrubbing:** env key/token literals and ``sk-...`` patterns redacted before
  write; ``scrubbed_fields`` recorded per interaction.
- **Wiring:** ``harness/providers.py::_finalize_driver`` wraps every
  ``build_pilot`` return; ``ConversationalSession`` wraps registry fallback
  drivers the same way.

## Tests

```bash
python -m pytest tests/test_cassette_driver.py -q
```

## Non-goals honored

- No HTTP-level VCR, cassette merging UI, or encryption at rest.
- No recording of tool execution or worker subprocess output.
