# Round 9 implementation plan (promoted-memory hygiene in Puppetmaster)

Instructions for the implementing model. All work happens in
`C:\Users\pwall\Projects\Puppetmaster`. Work the tasks IN ORDER. One commit
per task with the exact message given. Run the suite
(`python -m pytest tests -q`) before every commit. Commit locally only:
do NOT push, do NOT tag, do NOT publish, do NOT run `gh` — the user pushes
after review.

Ground rules (same as rounds 5-8):

- No emojis anywhere. Python 3.9 floor: no `match`, no `X | Y` unions in
  runtime annotations; `from __future__ import annotations` at module top.
- JSON, never YAML. No new dependencies.
- Best-effort seams swallow their own failures; anything on the worker
  dispatch hot path must never raise.
- Subprocess capture sets `encoding="utf-8", errors="replace"`.
- PowerShell: `;` separates commands, never `&&`.
- The working tree has pre-existing untracked files (`dist/`,
  `puppetmaster_ai.egg-info/`). Leave them alone; stage only files you
  create or edit for this plan.
- The evaluators CLI pattern from Wave 7 applies to any new subcommand:
  `--state-dir` is GLOBAL only; thread the dispatch-resolved `state_dir`
  parameter through, never re-parse it at the subcommand level.

## Context: why this wave

Waves 5-8 built the evaluator loop. Wave 9 fixes the OTHER durable-state
loop: promoted memory. Observed live on job dispatch today: the
orchestrator's `_with_retrieved_memory` injected five "memories" into an
implement worker, and four of them were worker role prompts echoed back
verbatim ("Role: token-efficiency-reviewer\nGoal: ... Return only
Puppetmaster artifact JSON with an artifacts array."). The fifth was a
stale, since-executed ship plan. Injecting this noise wastes tokens on
every dispatch and can actively mislead workers (a stale plan says "push
main and wait for CI").

Root causes, all confirmed in code:

1. Every adapter failure/timeout path stamps `check=task.instruction` on
   its VERIFICATION artifact (`adapters/_base.py` `_verification_artifact`
   and each adapter's error paths). `Stitcher._statement_for` then uses
   `payload["check"]` as the memory statement, so the whole worker prompt
   becomes a "memory statement".
2. `Stitcher._promote_memories` promotes ANY artifact with
   `confidence >= 0.8` and a non-empty statement. No content quality bar.
3. `FileStore.promote_memory` appends forever: no dedupe against existing
   promoted memory, no cap, no age-based expiry, no prune verb.
4. `retrieve_memory` scores by naive term overlap, so long prompt-echo
   statements match almost any goal and crowd out real findings
   (limit=5).

Read first: `puppetmaster/stitcher.py` (`_promote_memories`,
`_statement_for`, `_scope_for`), `puppetmaster/store.py`
(`promote_memory`, `list_memory`, `retrieve_memory`,
`_memory_matches_filters`), `puppetmaster/orchestrator.py`
(`_with_retrieved_memory`, `_memory_injection_enabled`,
`_FRESH_JUDGMENT_ROLES`), `puppetmaster/adapters/_base.py`
(`_verification_artifact`), `puppetmaster/models.py` (`MemoryRecord`),
and the CLI `memory` subcommand in `cli/_parser.py` + `cli/_dispatch.py`.

## Task A: design note

1. Add `docs/design_memory_hygiene.md` covering: the prompt-echo defect
   (with the four root causes above), the promotion quality gate
   (Task B), store-side dedupe/cap/expiry (Task C), the memory CLI
   surface (Task D), and non-goals: no embedding/vector retrieval, no
   LLM summarization of memories, no schema change to `MemoryRecord`
   beyond additive optional fields, no Redis/Postgres.
2. Commit: "Add design note for promoted-memory hygiene"

## Task B: promotion quality gate in the stitcher

Goal: stop instruction echoes and boilerplate from ever becoming memory.

1. In `puppetmaster/stitcher.py`:
   - Add a module-level helper `_is_instruction_echo(statement: str,
     artifact: Artifact) -> bool`: True when the statement is (after
     whitespace normalization) identical to, a prefix of, or contained in
     the artifact's originating task instruction — pass the instruction
     through the artifact payload when available. Since the stitcher only
     has artifacts, detect the echo structurally instead: treat as echo
     any VERIFICATION statement that (a) starts with `"Role:"`, or
     (b) contains any of the worker-boilerplate markers
     `"Return only Puppetmaster artifact JSON"`,
     `"Do not modify files unless"`, `"Return structured findings"`, or
     (c) exceeds 600 characters. Keep the marker list a module constant
     `_PROMPT_ECHO_MARKERS` so it is testable and extendable.
   - In `_promote_memories`, skip artifacts whose statement trips
     `_is_instruction_echo`. Also skip VERIFICATION artifacts whose
     payload `result` is one of `{"failed", "blocked", "degraded"}` —
     a failed check is job telemetry, not reusable knowledge.
   - Keep FINDING/DECISION promotion behavior otherwise unchanged.
2. Tests (extend the existing stitcher test class in
   `tests/test_puppetmaster.py`): prompt-echo verification is not
   promoted; a genuine short verification statement with result
   `"passed"` still is; failed/blocked verifications are not promoted;
   FINDING and DECISION promotion is unaffected; the 600-char guard
   trips.
3. Commit: "Gate memory promotion against instruction echoes and failed checks"

## Task C: store-side dedupe, cap, and expiry

Goal: the memory directory stops growing without bound and stale records
age out of retrieval.

1. In `puppetmaster/store.py` (`FileStore`; check whether
   `sqlite_store.py` overrides any of these and mirror there if so):
   - `promote_memory`: before writing, load existing memory and skip the
     write when a record with the same `scope` and
     whitespace-normalized `statement` already exists (return without
     error). After writing, enforce a cap of 200 records total: when
     over, delete the oldest records by `created_at` until at the cap.
   - `retrieve_memory`: add an optional `max_age_days: Optional[int] =
     None` parameter. When set, exclude records whose `created_at` is
     older. Parse `created_at` defensively (malformed dates are treated
     as fresh, never raise).
   - Add `prune_memory(self, *, scope: Optional[str] = None,
     older_than_days: Optional[int] = None) -> int`: delete matching
     records, return count. No filters means delete all promoted memory.
2. In `puppetmaster/orchestrator.py` `_with_retrieved_memory`: call
   `retrieve_memory` with `max_age_days=14` (module constant
   `_MEMORY_MAX_AGE_DAYS = 14`), overridable via env
   `PUPPETMASTER_MEMORY_MAX_AGE_DAYS` (0 or negative disables the age
   filter). Parse the env var defensively.
3. Tests: dedupe skips identical scope+statement; cap evicts oldest;
   `max_age_days` filters stale records and keeps fresh ones; malformed
   `created_at` is not excluded; `prune_memory` by scope, by age, and
   unfiltered returns correct counts; orchestrator injects only fresh
   memory (freeze time or write records with backdated `created_at`).
4. Commit: "Add promoted-memory dedupe, cap, and age-based expiry"

## Task D: memory CLI surface

Goal: inspect and clean promoted memory without hand-deleting JSON files.

1. Extend the existing `memory` subcommand (`cli/_parser.py`,
   `cli/_dispatch.py`) from a bare JSON dump into:
   - `python -m puppetmaster memory` — human-readable list grouped by
     scope: `<scope>: N record(s)` then one indented line per record:
     `[confidence] <first 120 chars of statement> (<created_at>)`.
   - `--json` — the current full JSON dump (preserve backward
     compatibility for anyone parsing it).
   - `--prune [--scope SCOPE] [--older-than-days N]` — call
     `prune_memory`, print `Pruned N memory record(s).` `--prune` with
     no filters requires `--yes` to confirm (refuse with a clear message
     otherwise, exit code 2).
   - Empty store prints `No promoted memory.`
2. Tests: list happy path + empty path + `--json` matches store
   contents; `--prune --scope` removes only that scope; unfiltered
   `--prune` without `--yes` refuses and deletes nothing; with `--yes`
   deletes all. Drive `cli_main` with the GLOBAL `--state-dir` flag and
   `contextlib.redirect_stdout` as the Wave 7/8 CLI tests do.
3. Commit: "Add memory list and prune CLI"

## Task E: release prep (bump, local commit, NO push)

1. Bump to `1.8.0` in `pyproject.toml`, `puppetmaster/__init__.py`,
   `README.md` (version line), and add a `## v1.8.0` section to
   `docs/CHANGELOG.md` summarizing Tasks B-D.
2. `python -m pytest tests -q` locally. The ~13 machine-local baseline
   failures on this Windows box (codegraph-invocation-shape and
   gate-command env tests) may be ignored ONLY if identical on a stashed
   baseline — anything newly failing is yours.
3. Commit: "chore(release): bump version to 1.8.0". Do NOT push, do NOT
   tag, do NOT publish, do NOT run `gh`.

## Verification (after ALL tasks)

```powershell
cd C:\Users\pwall\Projects\Puppetmaster
python -m pytest tests -q
git log --oneline -6
# five new local commits, tree clean apart from pre-existing untracked
# dist/ and egg-info/. NO PUSH. NO TAG. NO PYPI.
```

## Out of scope (do not do)

- Marionette repo changes.
- Embedding-based retrieval, LLM memory summarization, memory schema
  rewrites.
- Changing `_FRESH_JUDGMENT_ROLES` or memory-injection enablement.
- Touching the evaluator registry, drafts, gates, or anchors from
  Waves 5-8.
- Pushing anything to origin.

## After Wave 9 (roadmap)

- Marionette Round 8: consume the L0-L3 memory-layer snapshots from
  Round 7 (compaction advisor driven by layer pressure).
- Puppetmaster Wave 10 candidate: retrieval quality (scope-aware
  weighting so findings outrank verifications at injection time).
