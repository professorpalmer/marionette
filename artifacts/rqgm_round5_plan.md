# Round 5 implementation plan (RQGM evaluator lifts in Puppetmaster)

Instructions for the implementing model. **Work in the Puppetmaster repo**
(`C:\Users\pwall\Projects\Puppetmaster`), not Marionette. Work the tasks IN
ORDER. One commit per task with the exact message given. Run the full suite
(`python -m pytest tests/test_puppetmaster.py -q`) before every commit. Push
to Puppetmaster `main` at the end and wait for CI green. Do NOT tag either
repo in this round.

Ground rules:

- No emojis anywhere (code, comments, commits, docs). Plain words only.
- Python 3.9 floor: no `match`, no `X | Y` unions in runtime annotations;
  keep `from __future__ import annotations` at module top.
- SQLite writers open, write, and CLOSE per call (Windows file-lock lesson).
- Hot-path hooks swallow their own failures; never wrap the caller's logic.
- New behavior gets focused tests in `tests/test_puppetmaster.py`; mock
  subprocess/adapter calls the way existing gate and quality tests do.
- PowerShell shell: `;` separates commands, never `&&`.

Context: Marionette Waves 1-4 shipped the OMP tranche (tool discovery, internal
URIs, hash edits, LSP, savings ledger, memory offload, declarative checks with
`shell`/`file`/`artifact` kinds, session-level check overrides, worker-summary
surfacing) and staged Marionette **v0.7.45** on commit `34b0bf8`. Shepherd
(harness-side declarative checks) is complete for this arc. Wave 5 moves to
Puppetmaster and implements the **minimal RQGM kernel lifts** identified from
[The Red Queen Gödel Machine](https://arxiv.org/abs/2606.26294): evaluators as
durable, versioned citizens — not a full co-evolution research loop.

Read these Puppetmaster files before Task A (they are the integration anchors):

- `puppetmaster/workers.py` — `WorkerSpec`, `DEFAULT_WORKERS`, role graph
- `puppetmaster/orchestrator.py` — `_verification_confidence_by_task`,
  `_escalation_threshold`, job lifecycle
- `puppetmaster/gates.py` — `review` gate (proto-evaluator: stronger model
  judges a diff before COMPLETE)
- `puppetmaster/quality.py` — `assess_run_quality` (blocked/degraded/ok)
- `puppetmaster/adapters/_base.py` — `verification_artifact()` payload shape
- `puppetmaster/models.py` — `ArtifactType.VERIFICATION`, `ArtifactType.GATE`

## Task A: design note (contract only)

Goal: freeze the v1 RQGM shape before code. No runtime changes in this task.

1. Create `docs/design_rqgm_evaluators.md` in the Puppetmaster repo. Cover:
   - **Problem:** VERIFICATION artifacts and the `review` gate exist, but
     evaluator criteria are ad hoc per run — no durable slot, no version
     lineage, no frozen epoch, no promotion battery.
   - **Evaluator slot:** a named, versioned spec (`slot_id`, `version`,
     `role`, `instruction`, `criteria` dict, `parent_version`, `promoted_at`)
     stored under `{state_dir}/evaluators/`. Active slot is the highest
     version marked `active: true` per `slot_id`.
   - **Epoch freezing:** when a job starts, snapshot the active evaluator set
     into `job` metadata (or a dedicated EVALUATOR_EPOCH artifact). Mid-job
     registry edits do not affect tasks already running; only new jobs or an
     explicit epoch-advance boundary pick up changes.
   - **Anchor sets:** a JSON battery of deterministic tasks (local adapter,
     no LLM) with expected artifact shapes / gate outcomes. Promotion requires
     passing the battery at or above a configured threshold — never promote on
     a single lucky swarm.
   - **Integration map:** which orchestrator hook reads the epoch snapshot,
     which gate/review path consumes evaluator criteria, how VERIFICATION
     payloads gain an optional `evaluator_slot` + `evaluator_version` field.
   - **Non-goals:** full RQGM co-evolution, LLM-as-judge promotion, Marionette
     changes, new ArtifactType enums (use payload fields in v1).
2. Commit: "Add RQGM evaluator design note"

## Task B: evaluator slot registry v1

Goal: durable, versioned evaluator specs on disk with load/list/active helpers.

1. New module `puppetmaster/evaluators.py` (stdlib + existing store patterns):
   - `@dataclass(frozen=True) EvaluatorSpec`: `slot_id`, `version` (int),
     `role`, `instruction`, `criteria` (dict), `active` (bool),
     `parent_version` (optional int), `promoted_at` (optional ISO str).
   - `registry_path(state_dir) -> str` → `{state_dir}/evaluators/registry.json`
   - `load_registry(state_dir) -> list[EvaluatorSpec]` — missing file → `[]`;
     malformed file raises `ValueError` with path (callers on hot paths catch).
   - `save_registry(state_dir, specs)` — atomic write (temp + replace), owner
     restrict if `secure_files` helper exists in PM; else mirror sqlite_store
     write discipline.
   - `active_evaluators(state_dir) -> dict[str, EvaluatorSpec]` — one active
     spec per `slot_id` (highest version where `active` is true; tie-break by
     version).
   - `register_evaluator(state_dir, spec) -> EvaluatorSpec` — append version,
     deactivate prior active for same `slot_id`. Never mutate history in place.
2. Ship a checked-in seed file `docs/sample-evaluator-registry.json` with two
   slots mirroring existing swarm roles (`test` verifier, `redteam` reviewer)
   so tests and docs have a concrete example.
3. Tests in `tests/test_puppetmaster.py` (new class or module section):
   - round-trip load/save; missing registry → empty;
   - register bumps version and deactivates parent;
   - `active_evaluators` returns one per slot;
   - malformed registry raises cleanly.
4. Commit: "Add evaluator slot registry for RQGM v1"

## Task C: epoch snapshot at job start

Goal: freeze the active evaluator set for the lifetime of a job.

1. In `puppetmaster/orchestrator.py`, at job creation (find
   `create_job` / the code path that persists a new `Job` — read the file
   first), after the job row exists:
   - Call `active_evaluators(state_dir)` inside try/except (failure → skip;
     never block job creation).
   - Persist snapshot as a job-scoped artifact or job metadata JSON. v1
     recommendation: save one `Artifact` with `type=ArtifactType.DECISION`,
     `created_by="evaluator-registry"`, payload:

     ```json
     {"kind": "evaluator_epoch", "evaluators": [{"slot_id": "...", "version": 1, "role": "test"}]}
     ```

     Use existing `store.save_artifact`. Import `active_evaluators` inside
     the function.
2. Add `evaluator_epoch_for_job(store, job_id) -> dict` in
   `puppetmaster/evaluators.py` — reads the latest DECISION artifact with
   `payload.kind == "evaluator_epoch"` for that job; returns `{}` when absent.
3. Tests:
   - creating a job with a seeded registry writes an epoch artifact;
   - registering a new active evaluator AFTER job creation does not change
     the stored epoch for that job;
   - new job picks up the updated registry.
4. Commit: "Freeze evaluator epoch snapshot at job creation"

## Task D: anchor set battery + promotion CLI stub

Goal: deterministic promotion gate — no LLM judge for promotion in v1.

1. Add `docs/sample-anchor-set.json`: a list of `{ "id", "goal", "expect": {
   "min_verification_confidence": 0.8 } }` entries runnable against the
   **local** adapter only (reuse patterns from existing local adapter tests).
2. In `puppetmaster/evaluators.py`:
   - `load_anchor_set(path) -> list[dict]`
   - `run_anchor_battery(state_dir, anchor_path, *, slot_id) -> dict` with
     keys `passed`, `total`, `pass_rate`, `results[]`. v1 may simulate each
     anchor by calling the local adapter's verification path directly (read
     `puppetmaster/adapters/local.py` and `LocalWorker`) — do NOT spawn a
     full swarm. Each result records pass/fail + reason.
   - `promote_evaluator(state_dir, slot_id, *, parent_version, instruction,
     criteria, anchor_path, min_pass_rate=1.0) -> EvaluatorSpec` — runs
     battery; on success registers new active version; on failure raises
     `ValueError` with pass rate (caller-facing, not hot path).
3. CLI: add `python -m puppetmaster evaluators promote` and `... evaluators
   list` subcommands in the existing CLI parser (`puppetmaster/cli/` — mirror
   how `gate` or `models` subcommands are wired). `list` prints active slots;
   `promote` takes `--slot-id`, `--instruction`, optional `--anchor-set`,
   `--min-pass-rate`.
4. Tests: anchor battery pass/fail; promote succeeds only above threshold;
   promote writes new registry version.
5. Commit: "Add anchor-set promotion path for evaluator slots"

## Task E: wire evaluator metadata into VERIFICATION artifacts

Goal: downstream readers (orchestrator confidence, dashboard, Marionette
`artifact://` reads) can see which evaluator slot/version produced a check.

1. Extend `verification_artifact()` in `puppetmaster/adapters/_base.py` to
   accept optional `evaluator_slot: str = ""` and `evaluator_version: int = 0`;
   include in payload when non-empty/non-zero.
2. In `puppetmaster/worker_runtime.py`, when emitting verification results
   for a task, look up `evaluator_epoch_for_job` once per task (cached on the
   worker context if needed) and stamp matching slot metadata when the task
   `role` matches an evaluator spec's `role`. Swallow lookup failures.
3. Tests: verification artifact payload includes slot/version when epoch exists;
   omits fields when no epoch artifact.
4. Commit: "Stamp evaluator slot metadata on verification artifacts"

## Verification (after ALL tasks)

```powershell
cd C:\Users\pwall\Projects\Puppetmaster
python -m pytest tests/test_puppetmaster.py -q
git push origin main
# gh run watch <run-id> --exit-status
# NO TAG on either repo.
```

## Out of scope (do not do)

- Full RQGM co-evolution loop or genetic evaluator search.
- LLM-as-judge for promotion (anchor battery is deterministic in v1).
- Marionette / harness code changes (including declarative-check PM mirroring).
- New SQLite tables (registry is JSON v1; migrate later if needed).
- OMP medium-value leftovers (time-travel rules, advisor, AST preview,
  persistent eval) — optional Wave 6 in Marionette.
- TencentDB memory layering (already partially addressed by Marionette spill
  offload; deeper L0-L3 work is a separate track).
- YAML anywhere.

## After Wave 5 (roadmap)

- **Wave 6 (optional, Marionette):** OMP medium-value leftovers as a pick-list
  wave — only items with a written design note ship.
- **Wave 7+ (Puppetmaster):** evaluator-aware review gate (read epoch snapshot
  criteria instead of hardcoded judge prompt), dashboard surfacing of slot
  lineage, Redis/Postgres registry backend.
