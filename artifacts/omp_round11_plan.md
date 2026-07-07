# Round 11 implementation plan (append-only context / KV-cache reuse)

Instructions for the implementing model. All work happens in
`C:\Users\pwall\Projects\marionette`. Work the tasks IN ORDER. One commit
per task with the exact message given. Run the suite
(`python -m pytest tests -q`) before every commit. Commit locally only:
do NOT push, do NOT tag, do NOT publish, do NOT run `gh` — the user
pushes and ships after review.

Ground rules (same as rounds 5-10):

- No emojis anywhere. `from __future__ import annotations` at module top.
- JSON, never YAML. No new dependencies; stdlib only.
- Best-effort seams swallow their own failures; nothing on the chat hot
  path may raise.
- PowerShell: `;` separates commands, never `&&`.
- All file I/O: `encoding="utf-8"` explicitly.
- Stage only files you create or edit; leave pre-existing untracked
  files alone (including `artifacts/*_plan.md`).
- The oh-my-pi clone at `C:\Users\pwall\Projects\oh-my-pi` is read-only
  reference; never modify it.

## Context: why this round

Local model servers (Ollama, LM Studio, llama.cpp, vLLM, sglang) reuse
their prefix KV cache when the leading bytes of a request are identical
to the previous one; cache-discounting providers (DeepSeek-style) bill
cached prefix tokens at a fraction of the normal rate. Marionette
currently destroys that reuse every single step: in
`harness/conversation.py` the send loop rebuilds the system prompt each
attempt — `sys_prompt = base_sys` plus the per-turn CodeGraph section,
the MCP tools section, and the turn-budget note — and assigns it into
`self._history[0]["content"]`. Because message zero changes whenever the
user message changes (CodeGraph slice is keyed to `user_message`), the
prefix diverges at byte one and the whole conversation re-prefills every
turn.

OMP's reference implementation:
`packages/coding-agent/src/config/append-only-context-mode.ts` (provider
auto-detection: known local providers, loopback/RFC1918/.local base
URLs, DeepSeek, explicit opt-in) — read it first.

The lift: an append-only context mode that keeps the prompt prefix
byte-stable across turns by freezing the system prompt at session start
and moving all per-turn dynamic content into the LATEST user message
instead. History compaction still rewrites the prefix when it fires —
that is accepted (one cache miss per compaction, which the budget
already amortizes).

Read first: `harness/conversation.py` — the send-loop prompt assembly
(search for `sys_prompt = base_sys`, roughly line 2444), the CodeGraph
caching block just above it, `_render_history`, `_history[0]` usage,
`_maybe_compact_history`; `harness/turn_budget.py` (Round 10's
`_turn_budget_system_note` feeds the system prompt today);
`pmharness/registry.py` (how drivers/base URLs are configured, e.g.
`base_url`, `OPENROUTER_BASE`, native endpoints);
`artifacts/feature_flags.md` (flag doc format); and the OMP reference
file named above.

## Task A: design note

1. Create `artifacts/design_append_only_context.md`: the defect (system
   prompt churns per step, killing prefix KV reuse), the mode's
   contract (frozen prefix, dynamic content rides the newest user
   message, compaction is an accepted cache-miss point), the
   auto-detection rules (Task B), and non-goals: no provider-native
   cache APIs (Anthropic cache_control etc.), no change to compaction
   behavior, no reordering of existing history records, no frontend
   work this round.
2. Commit: "Add design note for append-only context mode"

## Task B: mode resolution module

1. New module `harness/append_only_context.py`:
   - `should_enable_append_only(setting: str, base_url: str, driver_name: str) -> bool`
     mirroring the OMP logic: `setting` is `"auto"` / `"on"` / `"off"`
     (`on` -> True, `off` -> False, `auto` -> detection).
   - Detection (auto): driver/provider name containing any of
     `ollama`, `lm-studio`, `lmstudio`, `llama.cpp`, `llamacpp`,
     `vllm`, `sglang`, `deepseek` (case-insensitive); OR base URL whose
     hostname is loopback (`localhost`, `127.0.0.1`, `0.0.0.0`, `::1`,
     `[::1]`), RFC1918 (`10.*`, `192.168.*`, `172.16-31.*`), or ends in
     `.local`. Parse with `urllib.parse`; unparseable URLs are False.
     Pure function, never raises.
   - `append_only_setting() -> str` reading `HARNESS_APPEND_ONLY_CONTEXT`
     (values `auto`/`on`/`off`/`1`/`0`/`true`/`false` normalized;
     default `auto`; garbage falls back to `auto`).
2. Tests (`tests/test_append_only_context.py`): each provider-name
   match; loopback, RFC1918, `.local`, public host, garbage URL; the
   three settings; env normalization including garbage.
3. Document `HARNESS_APPEND_ONLY_CONTEXT` in
   `artifacts/feature_flags.md`.
4. Commit: "Add append-only context mode resolution"

## Task C: byte-stable prefix in the send loop

Goal: with the mode active, the rendered prompt for step N+1 must start
with the exact rendered prompt bytes of step N (append-only), except
across a compaction event.

1. In `ConversationalSession` resolve the mode once per session (lazily
   on first send is fine): `self._append_only = should_enable_append_only(
   append_only_setting(), <driver base_url>, <driver name>)`. Get the
   base URL / driver name from the session's config/registry the same
   way existing code resolves prices (`pmharness.registry.resolve_price`
   neighborhood) — pick the accessor that already exists; do not invent
   a new config field.
2. When `self._append_only` is True, change prompt assembly in the send
   loop:
   - Freeze the system prompt: compute `base_sys + mcp_section` ONCE
     (first step of the first turn), store it, and never reassign
     `self._history[0]["content"]` after that. The MCP section is
     computed at freeze time; later MCP catalog changes do NOT rewrite
     the prefix (note this in the design doc).
   - The CodeGraph section and the turn-budget note move into the
     CURRENT turn's user message: append them under a clearly marked
     trailer (e.g. `\n\n[context for this turn]\n` + sections) to the
     user message content BEFORE it is added to `_history`, so history
     stays append-only and re-renders identically.
   - Nothing else about tool-call handling, retries, or compaction
     changes. When compaction rewrites history, the frozen prefix is
     recomputed once (the freeze applies from the new baseline).
   When the mode is False, behavior must be byte-identical to today —
   guard every change.
3. Tests (drive a fake pilot, capture the prompts it receives):
   - Mode ON: prompt for step 2 starts with the full prompt bytes of
     step 1 minus nothing (prefix property), across two user turns with
     a changing fake CodeGraph section (monkeypatch the codegraph import
     to return different sections per turn — assert they land in the
     user message, not the system prompt).
   - Mode ON: `_history[0]` content is identical across turns.
   - Mode OFF: existing behavior unchanged (reuse an existing prompt
     assembly test as the oracle if one exists; otherwise assert the
     turn note still lands in the system prompt).
   - Turn-budget hard/advisory behavior from Round 10 still passes.
4. Commit: "Keep prompt prefix byte-stable in append-only context mode"

## Task D: visibility

1. Add to the usage payload (`_context_usage` neighborhood, where
   `_turn_budget_usage_fields` was added in Round 10) an
   `append_only_context` boolean and a `prefix_stable_turns` counter:
   increments each turn the rendered prefix matched the previous turn's
   full prompt as a prefix, resets on compaction or mode off. Best
   effort, never raises.
2. Tests: counter increments across stable turns, resets on forced
   compaction, absent/false when mode off.
3. Commit: "Surface append-only context stability in usage payload"

## Task E: release prep (bump, local commit, NO push)

1. Bump to `0.9.0` in `pyproject.toml`, `harness/__init__.py`, README
   version line, `webapp/package.json` + lockfile version fields
   (mirror what the Round 10 bump commit touched — check
   `git show bbe20ac --stat`).
2. Full suite green locally (`python -m pytest tests -q`).
3. Commit: "chore(release): bump version to 0.9.0". NO push, NO tag.

## Verification (after ALL tasks)

```powershell
cd C:\Users\pwall\Projects\marionette
python -m pytest tests -q
git log --oneline -6
# five new local commits, tree otherwise clean. NO PUSH. NO TAG.
```

## Out of scope (do not do)

- Provider-native prompt-caching APIs (Anthropic cache_control,
  OpenAI prompt caching headers) — a later round.
- Changing compaction triggers or history schema.
- Frontend/webapp changes.
- Puppetmaster repo changes; oh-my-pi clone changes.
- Pushing anything to origin.
