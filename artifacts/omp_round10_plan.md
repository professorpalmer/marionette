# Round 10 implementation plan (OMP token-efficiency lift, Marionette)

Instructions for the implementing model. All work happens in
`C:\Users\pwall\Projects\marionette`. Work the tasks IN ORDER. One commit
per task with the exact message given. Run the suite
(`.venv\Scripts\python -m pytest tests -q`, or `python -m pytest tests -q`
if no venv) before every commit. Commit locally only: do NOT push, do NOT
tag, do NOT publish, do NOT run `gh` — the user pushes and ships after
review.

Ground rules (same as rounds 5-9):

- No emojis anywhere. `from __future__ import annotations` at module top.
- JSON, never YAML. No new dependencies; stdlib only.
- Best-effort seams swallow their own failures; nothing on the chat hot
  path may raise.
- PowerShell: `;` separates commands, never `&&`.
- All file I/O: `encoding="utf-8"` explicitly (Round 9 parity rule).
- Stage only files you create or edit; leave pre-existing untracked
  files alone.

## Context: why this round

We audited oh-my-pi (MIT, cloned at `C:\Users\pwall\Projects\oh-my-pi`)
for token-efficiency mechanisms. Marionette already has the savings
LEDGER (`harness/tool_output_savings.py`), a spill registry
(`harness/spill_registry.py`), and a history-compaction journal. What it
lacks are three OMP policies that make the machinery smarter:

1. The compaction advisor thinks purely in budget RATIOS. On a large
   context window (200k+) the session burns enormous absolute input
   tokens per request before any advice fires. OMP triggers at
   `min(percent_threshold, absolute_token_threshold / window)`
   (see `packages/coding-agent/src/modes/components/status-line/context-thresholds.ts`).
2. Tool-output compaction/spilling has no explicit SAVINGS GATE. OMP
   never touches results under a minimum token size and only swaps when
   the replacement costs at most 90 percent of the original
   (`packages/coding-agent/src/session/snapcompact-inline.ts`,
   `MIN_TOOL_RESULT_TOKENS = 3000`, `SAVINGS_MARGIN = 0.9`). A gate in
   one shared function means the applied policy and any savings estimate
   can never disagree.
3. No per-turn output budget. OMP lets the user write `+50k` in a
   message to set an advisory output-token budget for that turn, `+50k!`
   for a hard one (`packages/coding-agent/src/modes/turn-budget.ts`).

Read first: `harness/compaction_advisor.py` (whole file, it is short),
`harness/memory_layers.py` (snapshot shape), `harness/spill_registry.py`
and `harness/tool_output_savings.py` (existing offload + ledger seams and
where spills are applied from `harness/conversation.py`),
`harness/conversation.py` (`_maybe_compact_history`,
`_tool_output_compaction_callback`, how user messages enter the loop),
`artifacts/feature_flags.md` (flag doc format), and the OMP reference
files named above (read-only; the clone must not be modified).

## Task A: design note

1. Create `artifacts/design_omp_token_lift.md` describing the three
   policies above: source file in OMP, the mechanism, the Marionette
   seam it lands in, and non-goals (NO image rendering/snapcompact
   rasterization, NO embeddings, NO new storage, NO changes to what the
   ledger records).
2. Commit: "Add design note for OMP token-efficiency lift"

## Task B: absolute-token advisor thresholds

Goal: advice fires on large windows before the ratio thresholds do.

1. In `harness/compaction_advisor.py` add module constants
   `_HOT_NOW_TOKENS = 270_000` and `_HOT_SOON_TOKENS = 150_000`. In
   `assess_layer_pressure`, compute effective thresholds as
   `min(_HOT_NOW_RATIO, _HOT_NOW_TOKENS / budget)` and
   `min(_HOT_SOON_RATIO, _HOT_SOON_TOKENS / budget)` (guard budget > 0,
   already done). Keep the L1-combo rule as is but apply it against the
   effective soon threshold. When an absolute threshold is the binding
   one, the reason string must say so, e.g.
   "hot context above 150000 tokens on a large window".
2. Env overrides `HARNESS_ADVISOR_NOW_TOKENS` /
   `HARNESS_ADVISOR_SOON_TOKENS`, parsed defensively (invalid falls back
   to the constant; zero or negative disables the absolute rule so pure
   ratios apply). Document both in `artifacts/feature_flags.md`.
3. Tests (`tests/test_compaction_advisor.py` or the existing advisor
   test module): small window (say 32k budget) behaves exactly as
   before; 1M-token budget with L0 at 160k-token equivalent bytes
   returns "soon" even though the ratio is far below 0.55; env override
   zero restores old behavior; invalid env ignored.
4. Commit: "Trigger compaction advice on absolute token thresholds"

## Task C: shared savings gate for tool-output offload

Goal: one pure function decides every spill/compaction of a tool
result, with an explicit floor and margin, so tiny results are never
touched and every applied offload provably saves.

1. New module `harness/offload_policy.py`:
   - `MIN_TOOL_RESULT_TOKENS = 3000`, `SAVINGS_MARGIN = 0.9` module
     constants; env overrides `HARNESS_OFFLOAD_MIN_TOKENS` /
     `HARNESS_OFFLOAD_MARGIN` parsed defensively.
   - `should_offload(original_chars: int, replacement_chars: int) -> bool`:
     False when `estimate_tokens(original_chars)` (reuse
     `tool_output_savings.estimate_tokens`) is below the floor, or when
     `replacement_chars > original_chars * margin`. Pure, never raises.
   - `gate_decision(original_chars, replacement_chars) -> dict` returning
     `{"offload": bool, "reason": str, "estimated_tokens_saved": int}`
     for surfacing/tests.
2. Wire it into the existing call sites that shorten or spill tool
   outputs (follow `_tool_output_compaction_callback` usage and
   `spill_registry` writers from `harness/conversation.py`): before
   applying a spill/compaction, consult `should_offload`; when it says
   no, leave the output verbatim and record nothing. Behavior for
   results already below the floor must be a no-op (this is the point).
3. Tests: below-floor result never offloaded; replacement above margin
   rejected; big result with small stub accepted and the ledger records
   the saving once per (session, tool_call_id) (dedupe already exists in
   the ledger — assert it holds through the gate); env overrides work;
   gate never raises on garbage inputs (negative, zero, huge).
4. Document the two new flags in `artifacts/feature_flags.md`.
5. Commit: "Gate tool-output offload behind a savings floor and margin"

## Task D: per-turn token budget directive

Goal: `+Nk` in a user message sets an advisory output budget for that
turn; `+Nk!` makes it hard.

1. New module `harness/turn_budget.py` porting the OMP parser
   (`packages/coding-agent/src/modes/turn-budget.ts`) to Python:
   `parse_turn_budget(text: str) -> dict | None` returning
   `{"total": int, "hard": bool}`. Regex anchored to token boundaries:
   `(?:^|\s)\+(\d+(?:\.\d+)?)([km])?(!)?(?=\s|$)`, case-insensitive
   unit, so prices and version strings in prose never match. Non-finite
   or non-positive values return None.
2. In `harness/conversation.py`, when a user message arrives, parse a
   turn budget and stash it on the conversation for the turn. Advisory
   budget: include a single system-side note in the outgoing request
   ("output budget for this turn: N tokens") — no truncation. Hard
   budget: after each assistant response in the turn, if cumulative
   output tokens exceed the budget, stop the tool-call loop for that
   turn (finish the turn, do not raise) and surface
   "turn budget exhausted" in the turn's usage payload. Feature flag
   `HARNESS_TURN_BUDGET` default ON for parsing/advisory, document in
   `artifacts/feature_flags.md`.
3. Tests for the parser (`+50k`, `+1.5m!`, `+500`, embedded `+5k` inside
   a sentence matches only when whitespace-bounded, `$+5k`/`v+2` style
   non-matches, zero and negative rejected) and one conversation-level
   test that a hard budget stops the loop early (mock the driver).
4. Commit: "Add per-turn output token budget directive"

## Task E: release prep (bump, local commit, NO push)

1. Bump the patch/minor version following the existing scheme (check
   `pyproject.toml` and wherever the version currently lives; last
   release was 0.7.49 — bump to 0.8.0 since Tasks B-D add features).
   Update README version line and CHANGELOG if present.
2. Full suite green locally (`python -m pytest tests -q`); frontend
   untouched this round so no `npm run build` needed.
3. Commit: "chore(release): bump version to 0.8.0". NO push, NO tag.

## Verification (after ALL tasks)

```powershell
cd C:\Users\pwall\Projects\marionette
python -m pytest tests -q
git log --oneline -6
# five new local commits, tree otherwise clean. NO PUSH. NO TAG.
```

## Out of scope (do not do)

- Any change inside `C:\Users\pwall\Projects\oh-my-pi` (read-only
  reference).
- Puppetmaster repo changes.
- Snapcompact image rendering, vision, or PNG anything.
- Embeddings, new dependencies, schema changes to the savings ledger.
- Frontend changes (surfacing advice/budget in the webapp is a later
  round if wanted).
- Pushing anything to origin.
