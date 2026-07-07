# Wave 11 implementation plan (OMP retrieval + accounting lift, Puppetmaster)

Instructions for the implementing model. All work happens in
`C:\Users\pwall\Projects\Puppetmaster`. Work the tasks IN ORDER. One
commit per task with the exact message given. Run the suite
(`python -m pytest tests -q`) before every commit. Commit locally only:
do NOT push, do NOT tag, do NOT publish, do NOT run `gh` — the user
pushes and ships after review.

Ground rules (same as waves 5-10):

- No emojis anywhere. Python 3.9 floor: no `match`, no `X | Y` unions in
  runtime annotations; `from __future__ import annotations` at module top.
- JSON, never YAML. No new dependencies.
- Best-effort seams swallow their own failures; anything on the worker
  dispatch hot path must never raise.
- PowerShell: `;` separates commands, never `&&`.
- Stage only files you create or edit; leave pre-existing untracked
  files alone.
- Do not weaken any Wave 9 (hygiene) or Wave 10 (weighted ranking,
  injection floor) behavior.

## Context: why this wave

Two lifts from the oh-my-pi audit (MIT, read-only clone at
`C:\Users\pwall\Projects\oh-my-pi`) plus one defect observed live:

1. Wave 10's weighted ranking can still return five NEAR-DUPLICATE
   memories: nothing penalizes similarity among the selected set. OMP
   fixes this with MMR (maximal marginal relevance) reranking —
   `packages/mnemopi/src/core/mmr.ts` (~70 lines, pure): Jaccard
   word-set similarity plus a selection loop scoring
   `lambda * relevance - (1 - lambda) * max_similarity_to_selected`.
2. Memory injection has no cost accounting. Every worker prompt that
   gets promoted memory injected carries extra tokens that the savings
   ledger never sees. OMP logs each injection
   (`packages/mnemopi/src/core/cost-log.ts`): session, memory count,
   token count, estimated cost.
3. Live defect (job_c29e9124ed55): a four-worker analysis swarm on the
   agentic adapter ran every worker to `stop_reason == "max_turns"` with
   zero structured findings, yet the job completed looking successful —
   only a single alert line admitted failure. `puppetmaster/quality.py`
   already classifies degraded runs but only recognizes the CURSOR
   failure marker (`empty_or_unstructured_cursor_result`), not the
   agentic one (`empty_or_unstructured_agentic_result` emitted in
   `puppetmaster/adapters/agentic.py`).

Read first: `puppetmaster/store.py` (`retrieve_memory` with Wave 10
scoring), `puppetmaster/sqlite_store.py` (same seam),
`puppetmaster/orchestrator.py` (`_with_retrieved_memory`,
`_MEMORY_MIN_OVERLAP`), `puppetmaster/quality.py` (whole file, short),
`puppetmaster/adapters/agentic.py` (the degraded/verification artifact
around line 380 and the `max_turns` loop near line 730),
`puppetmaster/stitcher.py` (alerts section), `docs/design_memory_hygiene.md`,
and the OMP reference files named above (read-only).

## Task A: design note

1. Extend `docs/design_memory_hygiene.md` with a "Wave 11: diversity,
   cost accounting, degraded-run honesty" section covering the three
   items above and non-goals: embeddings/vector similarity, LLM
   reranking, automatic model escalation mid-job (detection and honest
   reporting only this wave), new memory schema fields.
2. Commit: "Extend design note for retrieval diversity and degraded-run honesty"

## Task B: MMR diversity rerank on memory retrieval

Goal: the injected set is relevant AND diverse — no near-duplicates.

1. New module `puppetmaster/mmr.py` porting the OMP algorithm:
   - `jaccard_similarity(text_a: str, text_b: str) -> float` on
     lowercased whitespace-split word sets.
   - `mmr_rerank(scored, lambda_param=0.7, top_k=10, similarity_fn=...)`
     where `scored` is a list of `(record, score)` pairs sorted or
     unsorted; returns records. Selection: highest score first, then
     repeatedly take the candidate maximizing
     `lambda * score - (1 - lambda) * max(similarity to selected)`.
     Backfill remaining slots in score order if the loop exhausts.
     Deterministic: ties broken by original order. Pure, never raises.
2. In `puppetmaster/store.py` `retrieve_memory` (and mirrored in
   `puppetmaster/sqlite_store.py` if it overrides): after the Wave 10
   composite scoring and floor, apply `mmr_rerank` over the top
   candidates (rerank the top `3 * limit` by score, return `limit`).
   Similarity text is the record's statement field. Env toggle
   `PUPPETMASTER_MEMORY_MMR` default on; `PUPPETMASTER_MEMORY_MMR_LAMBDA`
   parsed defensively (invalid falls back to 0.7; out of [0,1] clamped).
3. Tests: two near-identical statements plus one distinct — the distinct
   one makes the top set even with a slightly lower score; lambda=1.0
   reproduces pure score order; toggle off reproduces Wave 10 ordering
   exactly; determinism on ties; file and sqlite stores agree.
4. Commit: "Diversify memory retrieval with MMR reranking"

## Task C: memory injection cost log

Goal: every injection is accounted, so savings reporting is honest
about memory overhead.

1. In `puppetmaster/orchestrator.py` `_with_retrieved_memory`: when
   records are injected, log one entry — job id, task/role if available,
   record count, estimated tokens (chars/4 of the injected block), and
   estimated USD (reuse whatever price lookup the savings ledger already
   uses; 0.0 when unknown). Persist via the existing savings/state
   infrastructure (follow `puppetmaster/savings.py` conventions — same
   store, new entry kind such as `memory_injection`). Best-effort: a
   logging failure must never affect dispatch.
2. Surface totals in `python -m puppetmaster savings` output as a
   "memory injection overhead" line (count + tokens + USD), clearly
   labeled as spend, not savings. Disable knob
   `PUPPETMASTER_MEMORY_COST_LOG=0`.
3. Tests: an injection writes one entry with plausible token count; no
   injection writes nothing; failure inside logging does not raise into
   dispatch (monkeypatch the writer to throw); savings CLI includes the
   line when entries exist.
4. Commit: "Account memory injection overhead in the savings ledger"

## Task D: degraded agentic runs reported honestly

Goal: a swarm whose workers all died at max_turns with no findings must
classify as degraded, not sail through as a success.

1. In `puppetmaster/quality.py`, extend `_is_degraded_marker` (and any
   sibling logic) to also recognize
   `payload.get("failure") == "empty_or_unstructured_agentic_result"`.
   Additionally treat a verification artifact whose payload has
   `stop_reason == "max_turns"` AND no sibling substantive artifacts
   from the same worker as a degraded signal (follow the existing
   only-verification heuristic — extend, do not rewrite).
2. Ensure the stitched summary and `puppetmaster_status`/`show` surface
   the degraded classification prominently (the quality module already
   feeds them — verify the agentic path reaches the same reporting and
   add the minimal wiring if it does not).
3. In `puppetmaster/adapters/agentic.py`, when a worker stops at
   `max_turns` without submitting artifacts, include
   `"mitigation"` text in the degraded artifact payload advising a rerun
   with a higher-capability model or higher `max_turns` (mirror the
   cursor adapter's `cursor_degraded_artifact` wording). Detection and
   honest reporting only — no automatic escalation this wave.
4. Tests: an artifact set containing only agentic verification artifacts
   with the failure marker classifies degraded; mixed set with real
   findings does not; max_turns-with-no-findings classifies degraded;
   cursor behavior unchanged.
5. Commit: "Classify empty agentic max-turns runs as degraded"

## Task E: release prep (bump, local commit, NO push)

1. Bump to `1.10.0` in `pyproject.toml`, `puppetmaster/__init__.py`,
   `README.md` (version line), and add a `## v1.10.0` section to
   `docs/CHANGELOG.md` summarizing Tasks B-D.
2. `python -m pytest tests -q` locally. The ~13 machine-local baseline
   failures on this Windows box (codegraph-invocation-shape and
   gate-command env tests) may be ignored ONLY if identical on a stashed
   baseline — anything newly failing is yours.
3. Commit: "chore(release): bump version to 1.10.0". Do NOT push, do
   NOT tag, do NOT publish, do NOT run `gh`.

## Verification (after ALL tasks)

```powershell
cd C:\Users\pwall\Projects\Puppetmaster
python -m pytest tests -q
git log --oneline -6
# five new local commits, tree otherwise clean. NO PUSH. NO TAG. NO PYPI.
```

## Out of scope (do not do)

- Marionette repo changes; any change inside
  `C:\Users\pwall\Projects\oh-my-pi` (read-only reference).
- Embeddings, vector search, LLM reranking.
- Automatic mid-job model escalation or routing changes.
- Touching evaluators, gates, drafts, anchors, or Wave 9/10 behavior.
- Pushing anything to origin.
