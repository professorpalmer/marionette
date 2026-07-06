# Round 10 implementation plan (memory retrieval quality in Puppetmaster)

Instructions for the implementing model. All work happens in
`C:\Users\pwall\Projects\Puppetmaster`. Work the tasks IN ORDER. One
commit per task with the exact message given. Run the suite
(`python -m pytest tests -q`) before every commit. Commit locally only:
do NOT push, do NOT tag, do NOT publish, do NOT run `gh` — the user
pushes and ships after review.

Ground rules (same as rounds 5-9):

- No emojis anywhere. Python 3.9 floor: no `match`, no `X | Y` unions in
  runtime annotations; `from __future__ import annotations` at module top.
- JSON, never YAML. No new dependencies.
- Best-effort seams swallow their own failures; anything on the worker
  dispatch hot path must never raise.
- PowerShell: `;` separates commands, never `&&`.
- Stage only files you create or edit; leave any pre-existing untracked
  files alone.
- This wave builds directly on Wave 9's memory hygiene (dedupe, cap,
  age expiry, promotion quality gate). Do not weaken any Wave 9 behavior.

## Context: why this wave

Wave 9 stopped garbage from entering promoted memory. Wave 10 makes
retrieval smart about what it INJECTS. `FileStore.retrieve_memory`
(puppetmaster/store.py) scores by naive term overlap with confidence as
the only tiebreak. Consequences:

1. All scopes rank equally: a `swarm.verification` record ("check X
   passed") outranks a `swarm.findings` insight whenever it shares more
   words with the goal — but verifications are job telemetry, findings
   and decisions are the reusable knowledge.
2. Long statements win mechanically: more words means more term hits.
   Score is unnormalized by statement length.
3. Recency is ignored entirely: a six-month-old decision ties with
   yesterday's on the same overlap (Wave 9's age WINDOW filters old
   records out, but inside the window there is no ordering preference).

Wave 10 fixes ranking with deterministic arithmetic. No embeddings, no
LLM calls.

Read first: `puppetmaster/store.py` (`retrieve_memory`, `list_memory`,
`_memory_matches_filters`, and Wave 9's dedupe/cap/expiry additions),
`puppetmaster/sqlite_store.py` (check for overrides of any of those),
`puppetmaster/orchestrator.py` (`_with_retrieved_memory`,
`_MEMORY_MAX_AGE_DAYS`), `puppetmaster/stitcher.py` (`_scope_for` for the
scope vocabulary), `docs/design_memory_hygiene.md` (Wave 9's design note
this extends), and the Wave 9 memory test classes in
`tests/test_puppetmaster.py`.

## Task A: design note update

1. Extend `docs/design_memory_hygiene.md` with a "Wave 10: retrieval
   ranking" section covering: the three ranking defects above, the
   weighted-score formula (Task B) with its constants, the injection
   floor (Task C), and non-goals: embeddings/vector search, LLM
   reranking, changing the retrieval LIMIT, changing what gets promoted
   (that is Wave 9's seam), new memory schema fields.
2. Commit: "Extend memory hygiene design note for retrieval ranking"

## Task B: weighted retrieval scoring

Goal: findings and decisions outrank verification chatter; short precise
statements outrank long rambles; fresher wins ties.

1. In `puppetmaster/store.py` `retrieve_memory` replace the raw
   term-overlap score with a composite. Add module-level constants:
   - `_SCOPE_WEIGHTS = {"swarm.findings": 1.0, "swarm.decisions": 1.0,
     "swarm.general": 0.7, "swarm.verification": 0.4}` (unknown scopes
     use 0.7).
   - Overlap: `hits / max(1, len(terms))` — the fraction of QUERY terms
     matched, so long statements gain nothing mechanically. Keep the
     existing haystack fields and the len>2 term rule.
   - Recency: 1.0 for records <= 7 days old, linearly decaying to 0.5 at
     56 days and flat 0.5 beyond (`_RECENCY_FULL_DAYS = 7`,
     `_RECENCY_FLOOR = 0.5`, `_RECENCY_FLOOR_DAYS = 56`). Malformed
     `created_at` counts as fresh (mirror Wave 9's defensive parsing).
   - Final: `score = overlap * scope_weight * recency`, tiebreak by
     confidence then `created_at` descending (newest first) so ordering
     is fully deterministic.
   - Preserve the existing empty-query contract: when the query yields
     no terms, return records (up to limit) ranked by
     `scope_weight * recency` and confidence, and keep returning
     zero-overlap records ONLY in that empty-terms case, exactly like
     the current `if score > 0 or not terms` guard.
   - Keep the signature backward compatible (same parameters as after
     Wave 9, plus nothing new required).
2. Check `puppetmaster/sqlite_store.py`: if it overrides
   `retrieve_memory` or its scoring, mirror the change there; if it
   inherits, add a test proving the sqlite store returns the same
   ranking.
3. Tests: a findings record outranks a verification record with equal
   overlap; overlap normalization (a statement matching 3 of 3 query
   terms beats one matching 3 of many in a longer query — construct
   directly); fresh beats stale inside the age window; malformed
   `created_at` treated as fresh; empty-query path still returns records;
   deterministic order on exact ties (confidence then created_at).
3. Commit: "Rank memory retrieval by scope, precision, and recency"

## Task C: injection floor in the orchestrator

Goal: do not inject weak matches just because the limit allows five.

1. In `puppetmaster/orchestrator.py` `_with_retrieved_memory`: after
   retrieval, drop records whose composite score is below a minimum
   relevance. Since the store returns records (not scores), add an
   optional `min_overlap: float = 0.0` parameter to `retrieve_memory`
   instead: records with term overlap fraction below it are excluded
   before ranking (empty-terms queries are exempt so the empty-query
   contract holds). The orchestrator passes
   `min_overlap=_MEMORY_MIN_OVERLAP` (module constant `0.2`), env
   override `PUPPETMASTER_MEMORY_MIN_OVERLAP` parsed defensively
   (invalid values fall back to the constant; negative disables the
   floor).
2. Tests: a goal sharing no terms with any memory injects nothing (with
   the floor active); a strong match still injects; env override to 0
   restores old inject-anything behavior; invalid env value falls back
   silently.
3. Commit: "Add relevance floor to memory injection"

## Task D: release prep (bump, local commit, NO push)

1. Bump to `1.9.0` in `pyproject.toml`, `puppetmaster/__init__.py`,
   `README.md` (version line), and add a `## v1.9.0` section to
   `docs/CHANGELOG.md` summarizing Tasks B-C.
2. `python -m pytest tests -q` locally. The ~13 machine-local baseline
   failures on this Windows box (codegraph-invocation-shape and
   gate-command env tests) may be ignored ONLY if identical on a stashed
   baseline — anything newly failing is yours.
3. Commit: "chore(release): bump version to 1.9.0". Do NOT push, do NOT
   tag, do NOT publish, do NOT run `gh`.

## Verification (after ALL tasks)

```powershell
cd C:\Users\pwall\Projects\Puppetmaster
python -m pytest tests -q
git log --oneline -5
# four new local commits, tree otherwise clean. NO PUSH. NO TAG. NO PYPI.
```

## Out of scope (do not do)

- Marionette repo changes.
- Embedding or vector retrieval, LLM reranking.
- Changing the retrieval limit, promotion rules, or Wave 9 hygiene.
- Touching evaluators, gates, drafts, or anchors.
- Pushing anything to origin.

## After Wave 10

This is the last planned wave of the arc. The user ships v1.9.0
(tag + GitHub release + PyPI) after review; future work is driven by
real usage, not planned waves.
