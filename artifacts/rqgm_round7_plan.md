# Round 7 implementation plan (evaluator-aware review gate in Puppetmaster)

Instructions for the implementing model. All work happens in
`C:\Users\pwall\Projects\Puppetmaster`. Work the tasks IN ORDER. One commit
per task with the exact message given. Run the suite
(`python -m pytest tests -q`, or `python -m unittest discover -s tests -q`
to mirror CI) before every commit. Push to main at the end and wait for CI
green on ALL legs (ubuntu 3.9/3.12, macOS 3.12, windows 3.12). Do NOT tag —
the user cuts `v1.6.0` after review.

Ground rules (same as rounds 5-6):

- No emojis anywhere. Python 3.9 floor: no `match`, no `X | Y` unions in
  runtime annotations; `from __future__ import annotations` at module top.
- JSON, never YAML. No new dependencies.
- Best-effort hooks swallow their own failures (mirror
  `_snapshot_evaluator_epoch`); gates FAIL CLOSED on their own errors
  (mirror `_evaluate_one`'s except branch). Know which of the two seams you
  are in before choosing an error posture.
- Subprocess capture sets `encoding="utf-8", errors="replace"` when adding
  any new subprocess call (Windows cp1252 lesson).
- Windows: never `os.kill(pid, 0)` as a liveness probe (it is CTRL_C_EVENT);
  use the `OpenProcess` helpers that already exist in `liveness.py` /
  `codegraph.py`.
- PowerShell shell: `;` separates commands, never `&&`.

Context: Wave 5 (v1.5.x) shipped the RQGM evaluator foundation: a versioned
slot registry (`puppetmaster/evaluators.py`), an epoch snapshot frozen as a
job-scoped DECISION artifact at job creation (`orchestrator.py`
`_snapshot_evaluator_epoch`), deterministic anchor-set promotion
(`evaluators list|promote` CLI), and slot/version stamping on VERIFICATION
artifacts. But the epoch is still lineage-only (slot_id/version/role) and
nothing *consumes* it: the review gate (`gates.py` `_gate_review`) judges
diffs with a hardcoded rubric (`_DEFAULT_REVIEW_RUBRIC`). Wave 7 closes the
loop: the epoch carries the evaluator's actual criteria, the review gate
prefers those frozen criteria over the hardcoded rubric, and the lineage is
visible in the dashboard and CLI.

Read first (in this order): `puppetmaster/evaluators.py` (all of it),
`orchestrator.py` `_snapshot_evaluator_epoch` (~line 184), `gates.py` lines
100-160 (`task_gate_specs`) and 407-700 (the whole review-gate section),
`docs/design_rqgm_evaluators.md`, and the Wave 5 test classes in
`tests/test_puppetmaster.py` (`EvaluatorRegistryTests`, `EvaluatorEpochTests`,
`EvaluatorVerificationStampTests`) — new tests join those.

## Task A: design note update

1. Extend `docs/design_rqgm_evaluators.md` with a "Wave 7: consuming the
   epoch" section covering: full-fidelity epoch payloads (criteria +
   instruction frozen, not just lineage), rubric resolution order for the
   review gate (explicit gate-spec `rubric` > epoch slot criteria > default
   rubric), and lineage surfacing (dashboard + CLI). Name the non-goals:
   no per-task evaluator overrides, no automatic re-review when a slot is
   promoted mid-job (the epoch freeze already forbids it), no registry
   backends beyond JSON (Redis/Postgres explicitly deferred again).
2. Commit: "Extend RQGM design note for evaluator-aware review gate"

## Task B: full-fidelity epoch snapshot

Goal: the frozen epoch must carry what the evaluator actually says, or the
review gate would read live registry state and break the freeze guarantee.

1. In `orchestrator.py` `_snapshot_evaluator_epoch`, extend each entry in
   `evaluators` with `"instruction": spec.instruction` and
   `"criteria": dict(spec.criteria)` (note: `EvaluatorSpec.criteria` is a
   dict, not a list — keep the existing three fields; order stays sorted by
   slot_id).
2. In `puppetmaster/evaluators.py`, add
   `epoch_evaluator_for_role(epoch: dict, role: str) -> dict` returning the
   first evaluator entry whose `role` matches (case-insensitive), else `{}`.
   Pure function, no I/O — this is the lookup the gate and the stamper share.
   Refactor `stamp_verification_artifacts` to use it (behavior unchanged).
3. Backward compatibility: `epoch_evaluator_for_role` must tolerate Wave 5
   epochs whose entries lack `instruction`/`criteria` (missing keys — return
   the entry as-is; callers treat absent criteria as "no frozen rubric").
4. Tests: epoch artifact now carries instruction + criteria; role lookup
   hits and misses; Wave 5-shaped epoch (no criteria) does not break the
   stamper or the lookup.
5. Commit: "Freeze evaluator instruction and criteria in the job epoch"

## Task C: evaluator-aware review gate

Goal: `_gate_review` judges with the epoch's frozen criteria when available.

1. In `gates.py` `_gate_review`, resolve the rubric in this order:
   a. explicit `spec.get("rubric")` on the gate spec (unchanged, wins);
   b. the job's epoch: load `evaluator_epoch_for_job(store, task.job_id)`
      and look up `epoch_evaluator_for_role(epoch, "review")` (fall back to
      the task's own `task.role` when the "review" slot misses); when the
      entry has non-empty `criteria` (a dict), render them as the rubric —
      one line per item as `- <key>: <value>`, sorted by key, preceded by
      the entry's `instruction` when non-empty;
   c. `_DEFAULT_REVIEW_RUBRIC` (unchanged fallback).
   Note `_gate_review` does not currently receive the store — thread it
   through from `evaluate_task_gates` / `_evaluate_one` (they already have
   `store`). Epoch lookup failures must NOT fail the gate (this is rubric
   *selection*, not gate execution): wrap the lookup so any exception falls
   back to (c). The gate's own fail-closed posture stays untouched.
2. Stamp provenance: when an epoch rubric is used, add
   `"rubric_source": "evaluator_epoch"`, `"evaluator_slot"`, and
   `"evaluator_version"` to the GateResult detail dict (the GATE artifact
   already persists detail). When not, `"rubric_source": "spec"` or
   `"default"`.
3. Tests (fake judge via the `_REVIEW_JUDGE` seam, as existing review-gate
   tests do): epoch criteria land in the judge prompt; explicit spec rubric
   still wins over the epoch; no epoch → default rubric; epoch lookup
   raising → default rubric and the gate still runs; detail carries
   rubric_source/slot/version.
4. Commit: "Review gate reads frozen evaluator criteria from the job epoch"

## Task D: lineage surfacing (dashboard + CLI)

Goal: "which evaluator judged this job, at what version" is answerable
without reading raw artifacts.

1. Dashboard: in `puppetmaster/dashboard.py` `build_job_snapshot`, add an
   `evaluator_epoch` field to the snapshot: the epoch payload's `evaluators`
   list (slot_id/version/role only — not the criteria bodies) or `[]`.
   Follow how existing snapshot fields degrade: missing/broken epoch reads
   yield `[]`, never an exception. Render: one compact line/badge in the
   job view listing `slot@vN (role)` per entry — copy the styling of an
   existing metadata row rather than inventing new UI.
2. CLI: add `python -m puppetmaster evaluators epoch <job_id>` to
   `puppetmaster/cli/commands_evaluators.py` (+ `_parser.py`): prints the
   job's frozen evaluator set (slot, version, role, criteria count) or
   "No evaluator epoch recorded." — read-only, uses
   `find_state_dir_for_job` the way other job-scoped commands do.
3. Tests: snapshot includes the epoch list when present and `[]` when not;
   CLI epoch subcommand happy path + missing-epoch path (drive `_main` the
   way existing evaluators CLI tests do).
4. Commit: "Surface evaluator epoch lineage in dashboard and CLI"

## Task E: release prep (bump, push, NO tag, NO publish)

1. Bump to `1.6.0` in `pyproject.toml`, `puppetmaster/__init__.py`,
   `README.md` (version line), and add a `## v1.6.0` section to
   `docs/CHANGELOG.md` summarizing Tasks B-D.
2. Full verification: `python -m pytest tests -q` green locally. Known
   machine-local failures on this Windows box (codegraph-invocation-shape
   and gate-command env tests that fail at baseline too) may be ignored ONLY
   if they fail identically on a stashed baseline — anything newly failing
   is yours. CI is the source of truth.
3. Commit: "chore(release): bump version to 1.6.0", push to main, wait for
   CI green on all four legs (`gh run watch <id> --exit-status`). Report the
   run id.
4. Do NOT tag, do NOT publish to PyPI, do NOT create a GitHub release —
   those are user-owned steps after review.

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
- Redis/Postgres registry backends (JSON registry stays).
- Per-task evaluator overrides or mid-job re-review on promotion.
- LLM-as-judge inside anchor promotion (stays deterministic).
- Changing the review gate's enablement, sampling, or judge-selection
  logic — Wave 7 only changes where the rubric text comes from.
- YAML anywhere. Pushing any tag.

## After Wave 7 (roadmap)

- **Wave 8 (Puppetmaster):** evaluator feedback loop — failed review-gate
  verdicts append candidate criteria to a slot's draft next version, gated
  behind the existing anchor battery for promotion.
- **Wave 9 (either):** TencentDB-style L0-L3 memory layering study — design
  note first, building on Marionette spill-offload measurements.
