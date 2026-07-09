# Session decisions — 2026-07-09 (v0.9.24)

## Drop trailing REASONING block (token + UI waste)

- Symptom: after the pilot answer streamed in, a collapsible "REASONING"
  block appeared below it and repeated the same content.
- Cause: (1) OpenAI-compat drivers defaulted `enable_reasoning=True` and
  requested `reasoning.max_tokens=1024` on every chat call; (2) the
  conversation loop emitted a late `thinking` ConvEvent after streaming
  already painted the answer; (3) worker-stream previews were converted
  into a permanent thinking row on `action_result`.
- Fix: default `enable_reasoning=False`; stop emitting thinking events;
  ignore thinking SSE on the frontend; drop worker-stream bubbles instead
  of converting them to reasoning. Answer-only transcript.
