# Round 8 implementation plan (evaluator feedback loop in Puppetmaster)

Instructions for the implementing model. All work happens in
`C:\Users\pwall\Projects\Puppetmaster`. Work the tasks IN ORDER. One commit
per task with the exact message given. Run the suite
(`python -m pytest tests -q`) before every commit. Push to main at the end
and wait for CI green on ALL legs. Do NOT tag — the user cuts `v1.7.0`
after review.

Ground rules (same as rounds 5-7):

- No emojis anywhere. Python 3.9 floor: no `match`, no `X | Y` unions in
  runtime annotations; `from __future__ import annotations` at module top.
- JSON, never YAML. No new dependencies.
- Best-effort hooks swallow their own failures (mirror
  `_snapshot_evaluator_epoch`); gates FAIL CLOSED on their own errors.
  Rubric/draft *selection and recording* are best-effort seams; gate
  *execution* is fail-closed. Know which seam you are in.
- Subprocess capture sets `encoding="utf-8", errors="replace"`.
- Windows: never `os.kill(pid, 0)` as a liveness probe; use the
  `OpenProcess` helpers in `liveness.py`.
- PowerShell: `;` separates commands, never `&&`.
- DECISION artifacts require `payload.decision` AND `payload.why`
  (`models.py` validates on save — Wave 7 tests tripped on this twice).
- The evaluators CLI takes `--state-dir` only as the GLOBAL flag
  (`puppetmaster --state-dir X evaluators ...`); the subcommand-level flag
  was removed in Wave 7. `_run_evaluators_subcommand(args, state_dir=...)`
  receives the dispatch-resolved state dir — thread new subcommands the
  same way.

Context: Wave 7 (v1.6.0) closed the consumption loop: the job epoch freezes
each evaluator's `instruction` + `criteria`, `_gate_review` resolves its
rubric as spec > frozen epoch (via `epoch_evaluator_for_role`) > default,
and GATE artifacts carry `rubric_source` / `evaluator_slot` /
`evaluator_version` provenance. What is still missing is the RQGM
*improvement* half: when the review gate rejects work, the judge's reasons
are discarded after the task fails. Wave 8 turns rejections into **draft
criteria** for the evaluator's next version — accumulated durably, reviewed
by a human, and promoted only through the existing deterministic anchor
battery. No self-modifying evaluator: the loop proposes, the battery + user
dispose.

Read first: `puppetmaster/evaluators.py` (registry + epoch helpers),
`gates.py` `_resolve_review_rubric` / `_gate_review` (lines ~648-745) and
`_gate_artifact`, `worker_runtime.py` (where gates run), and the Wave 7
test classes (`ReviewGateTests` epoch tests, `EvaluatorEpochSurfacingTests`).

## Task A: design note update

1. Extend `docs/design_rqgm_evaluators.md` with a "Wave 8: the feedback
   loop" section covering: draft criteria records (what, where, shape),
   the capture hook (failed review verdicts with epoch provenance), the
   human-in-the-loop promotion path (`evaluators drafts` to inspect,
   existing `evaluators promote` to adopt — the battery still gates), and
   dedupe/caps. Non-goals: automatic promotion (never), LLM synthesis of
   criteria text (verbatim judge reasons only, v1), draft records for
   spec-rubric or default-rubric rejections (only epoch-sourced reviews
   feed the slot that produced them), registry backends beyond JSON.
2. Commit: "Extend RQGM design note for the evaluator feedback loop"

## Task B: draft criteria store

Goal: a durable, append-capped journal of candidate criteria per slot.

1. In `puppetmaster/evaluators.py` add:
   - `drafts_path(state_dir: str) -> str` returning
     `{state_dir}/evaluators/drafts.json`.
   - `record_draft_criteria(state_dir, *, slot_id, source_job_id,
     source_task_id, reasons, severity) -> bool`: append one draft record
     per call to the JSON file (shape:
     `{"slot_id", "reasons": [...], "severity", "source_job_id",
     "source_task_id", "recorded_at"}` under a root `{"drafts": [...]}`).
     Atomic write via the same tmp + `os.replace` pattern as
     `save_registry`, using `write_private_text`. Rules:
     - Skip empty/whitespace-only reasons; skip if no reasons survive.
     - Dedupe: if an existing draft for the same `slot_id` has an
       identical sorted reasons list, do not append (return False).
     - Cap: at most 50 drafts per slot; when full, drop the OLDEST draft
       for that slot to admit the new one.
     - Never raises: any error returns False (this is a best-effort seam).
   - `load_drafts(state_dir, slot_id=None) -> list[dict]`: all drafts,
     optionally filtered by slot; missing/corrupt file returns `[]`.
   - `clear_drafts(state_dir, slot_id) -> int`: remove that slot's drafts,
     return the count removed.
2. Tests: record + load round trip; dedupe on identical reasons; cap
   eviction (oldest out); corrupt drafts file loads as `[]` and a
   subsequent record still succeeds; clear returns the removed count.
3. Commit: "Add draft criteria store for evaluator feedback"

## Task C: capture failed review verdicts

Goal: an epoch-rubric rejection writes a draft for the slot that judged it.

1. In `gates.py` `_gate_review`, after building the failing GateResult
   (the `not verdict.passed` branch): when `rubric_meta` says
   `rubric_source == "evaluator_epoch"` and `verdict.reasons` is
   non-empty, call `record_draft_criteria` with the slot/version from
   `rubric_meta`, `task.job_id`, `task.id`, `verdict.reasons`, and
   `verdict.severity`. Resolve the state dir from `store.root` exactly the
   way `_snapshot_evaluator_epoch` does (`getattr(store, "root", None)`).
   Wrap the whole capture in try/except-pass — recording must never change
   the gate verdict or crash the gate. Add
   `"draft_recorded": <bool>` to the failing GateResult detail.
   Do NOT record for passes, spec rubrics, default rubrics, unavailable
   judges, or unparseable-verdict rejections (those have no reasons from a
   real epoch-driven review; unparseable verdicts produce the synthetic
   "judge produced no parseable verdict" reason — exclude it by checking
   `rubric_source`, which already covers this since unparseable verdicts
   still carry the epoch rubric_meta: ALSO skip when
   `verdict.detail.get("raw_verdict") is None`).
2. Tests (fake judge via `_REVIEW_JUDGE` seam, epoch saved as a DECISION
   artifact with `decision` + `why` like Wave 7's `_save_epoch` helper):
   epoch-rubric rejection writes a draft with the right slot and reasons;
   pass writes nothing; spec-rubric rejection writes nothing;
   unparseable-verdict rejection writes nothing; a draft-store error does
   not change the gate verdict (patch `record_draft_criteria` to raise);
   detail carries `draft_recorded`.
3. Commit: "Capture failed epoch review verdicts as draft criteria"

## Task D: drafts CLI + promote handoff

Goal: inspect the accumulated drafts and fold them into a promotion.

1. CLI: add `python -m puppetmaster evaluators drafts [slot_id] [--json]
   [--clear SLOT_ID]` to `commands_evaluators.py` + `_parser.py`:
   - No args: list drafts grouped by slot —
     `<slot_id>: N draft(s)` then one indented line per draft:
     `[severity] reason1; reason2 (job <source_job_id>)`.
   - With `slot_id`: only that slot's drafts.
   - `--json`: machine-readable dump of the same records.
   - `--clear SLOT_ID`: call `clear_drafts`, print
     `Cleared N draft(s) for <slot_id>.` Mutually exclusive with `--json`.
   - Empty store prints `No draft criteria recorded.`
   - Thread `state_dir` through `_run_evaluators_subcommand` like the
     Wave 7 `epoch` subcommand does.
2. Promote handoff: add `--from-drafts` flag to `evaluators promote`.
   When set, load the slot's drafts and merge their reasons into the new
   version's `criteria` dict as `{"draft_note_<n>": "<reason>"}` entries
   (n = 1.., insertion order job-then-reason, capped at 10 notes),
   layered UNDER any explicit `--criteria-json` keys (explicit wins on
   collision). Promotion still runs the anchor battery exactly as before —
   a failing battery still refuses. On successful promotion with
   `--from-drafts`, clear that slot's drafts and print how many were
   folded in and cleared.
3. Tests: drafts listing happy path + empty path + `--json` + `--clear`
   (drive `cli_main` with `--state-dir` GLOBAL flag and
   `contextlib.redirect_stdout`, as `EvaluatorEpochSurfacingTests` does);
   promote `--from-drafts` folds notes into criteria, explicit criteria
   win on collision, drafts cleared after success, drafts NOT cleared
   when the battery fails.
4. Commit: "Add evaluator drafts CLI and promote --from-drafts handoff"

## Task E: release prep (bump, push, NO tag, NO publish)

1. Bump to `1.7.0` in `pyproject.toml`, `puppetmaster/__init__.py`,
   `README.md` (version line), and add a `## v1.7.0` section to
   `docs/CHANGELOG.md` summarizing Tasks B-D.
2. `python -m pytest tests -q` locally. The ~13 machine-local baseline
   failures on this Windows box (codegraph-invocation-shape and
   gate-command env tests) may be ignored ONLY if identical on a stashed
   baseline — anything newly failing is yours. CI is the source of truth.
3. Commit: "chore(release): bump version to 1.7.0", push to main, watch
   CI (`gh run list --branch main --limit 1 --json databaseId` then
   `gh run watch <id> --exit-status`). Report the run id.
4. Do NOT tag, do NOT publish to PyPI, do NOT create a GitHub release.

## Verification (after ALL tasks)

```powershell
cd C:\Users\pwall\Projects\Puppetmaster
python -m pytest tests -q
git push origin main
# gh run watch <run-id> --exit-status  (all legs green)
# NO TAG. NO PYPI.
```

## Out of scope (do not do)

- Marionette repo changes.
- Automatic evaluator promotion — every promotion stays human-invoked and
  battery-gated.
- LLM-driven synthesis/rewriting of draft criteria text.
- Changing review-gate enablement, sampling, judge selection, or the
  Wave 7 rubric resolution order.
- Redis/Postgres registries. YAML anywhere. Pushing any tag.

## After Wave 8 (roadmap)

- **Wave 9 (either repo):** TencentDB-style L0-L3 memory layering study —
  design note first, building on Marionette spill-offload measurements.
- **Marionette backlog resumes** after the Puppetmaster feedback loop
  lands, per the user's sequencing.
