# Round 4 implementation plan (Shepherd completion + release prep)

Instructions for the implementing model. Work the tasks IN ORDER. One commit
per task with the exact message given. Run the full suite
(`.\.venv\Scripts\python.exe -m pytest -q`) before every commit; run
`npm run build` in `webapp/` additionally when a task touches frontend files.
Push to main at the end and wait for CI green. Do NOT create any release tag —
Task D stages the version bump, but tagging is a separate user-owned step.

Ground rules (same as rounds 1-3):

- No emojis anywhere. stdlib-only for the rig (`urllib`, `sqlite3`,
  `dataclasses`, `subprocess`). JSON, never YAML.
- SQLite writers open, write, and CLOSE per call (Windows file-lock lesson;
  copy `harness/spill_registry.py`).
- Hot-path hooks swallow their own failures; never wrap the caller's logic.
- Python 3.9 floor: no `match`, no `X | Y` unions in runtime annotations;
  keep `from __future__ import annotations` at module top.
- PowerShell shell: `;` separates commands, never `&&`.

Context: Rounds 1-3 shipped the OMP tranche, memory offload (`spill://`),
declarative checks v1 (`shell`/`file` kinds), and provider cassettes. This
round closes the Shepherd design (`artifacts/design_declarative_checks.md`)
by adding the deferred `artifact` check kind and surfacing check results in
the UI, then stages the release bump.

## Task A: `artifact` check kind

Goal: post checks can assert Puppetmaster artifacts exist for the job the
worker ran under (e.g. "a PATCH artifact was produced").

1. Read first: `harness/declarative_checks.py` (all of it),
   `harness/internal_uri.py` — specifically `InternalUriContext.store()` and
   how `_resolve_artifact` lists artifacts via `store.list_artifacts(job_id)`.
2. Extend `harness/declarative_checks.py`:
   - Add `"artifact"` to `_VALID_KINDS`. New `CheckSpec` fields:
     `artifact_type: str = ""` (e.g. `"PATCH"`, matched case-insensitively
     against the artifact's `type` name) and `min_count: int = 1`.
     Spec JSON shape:

     ```json
     {"id": "patch-present", "kind": "artifact", "on_fail": "failed",
      "expect": {"type": "PATCH", "min_count": 1}}
     ```

     Parse the nested `expect` object in `_parse_check_item`; missing or
     empty `expect.type` raises ValueError naming the check id. `artifact`
     checks are only meaningful post-run: reject `phase == "pre"` for this
     kind with a ValueError (a job has no artifacts before the worker runs).
   - `run_checks` gains keyword-only params `state_dir: str = ""` and
     `job_id: str = ""`. New `_run_artifact_check(spec, state_dir, job_id)`:
     when either is empty, return a failed result with output
     `"artifact check requires a job context"` — never raise. Otherwise build
     the store exactly like `InternalUriContext.store()` does
     (`create_store("sqlite", state_dir)`; import inside the function), call
     `store.list_artifacts(job_id)`, count artifacts whose type name matches
     `artifact_type` case-insensitively (artifact `type` may be an enum —
     compare against `getattr(a.type, "name", str(a.type))`), pass when
     `count >= min_count`. Wrap the whole store interaction in try/except:
     any exception becomes a failed result carrying the exception text.
3. Plumb the job context in `harness/worker.py`: the post-check call site
   already exists (`_run_impl`, search for `phase="post"`). Pass
   `state_dir=base_cfg.state_dir` (the worker builds `base_cfg` from env
   earlier in the method — reuse it) and `job_id=self.job_id`. Pre checks
   need no plumbing (artifact kind is post-only).
4. Tests in `tests/test_declarative_checks.py`:
   - Loader: `expect` parsing round-trip; missing `expect.type` rejected;
     `artifact` in `pre` phase rejected.
   - Runner without job context: failed result, no exception.
   - Runner with a real store: build one the way `tests/test_internal_uri.py`
     seeds Puppetmaster state (find its fixture that creates a store with a
     job and artifacts and copy the approach); assert pass at
     `min_count=1` for a type that exists and fail for a type that does not.
     If seeding a real store proves heavier than one fixture, a stub object
     with a `list_artifacts` method monkeypatched into the store factory is
     acceptable — deterministic either way.
5. Update `artifacts/design_declarative_checks.md`: remove the "artifact
   check kind deferred" line from non-goals and note it shipped post-only.
   Update `artifacts/declarative_checks_acceptance.md` the same way.
6. Commit: "Add artifact check kind for declarative post checks"

## Task B: surface check results in the swarm UI

Goal: failed declarative checks are visible where job results are read,
per the design note's UI integration point.

1. Read first: in `harness/server.py`, how `/api/swarm/live` builds
   `res_jobs` entries and how `_job_savings_fields` merges per-job data
   (round 2 Task D added it — mirror that pattern exactly). Then find where
   `WorkerResult` flows back into the conversation/session after
   `run_implement` (search `conversation.py` for `declarative_checks` uses;
   there are none yet — find where `result.summary` / `result.error` are
   consumed and stored).
2. Persistence: worker results are transient, so surface via the session.
   In `harness/conversation.py`, where the implement-worker result is folded
   into the transcript/summary, append a compact line to the result text the
   model and UI already see when any check failed:
   `"Declarative checks: N failed (id1, id2)"`. Passing checks stay silent.
   Do NOT invent a new persistence store for check results in this round.
3. Frontend: no new pane. The check-failure line rides inside the existing
   worker summary text that SwarmPane/transcript already render. Verify with
   a conversation-level test, not a UI test.
4. Tests: extend the worker-level test in `tests/test_declarative_checks.py`
   to assert a failed post check's ids appear in the summary/error text the
   session receives (drive it the same way the existing blocked-pre-check
   test drives `ProviderWorker` with a mocked `run_auto`).
5. Commit: "Surface failed declarative checks in worker summaries"

## Task C: session-level check overrides

Goal: the design note's third spec location — harness-only runs can carry
checks without touching the repo.

1. In `harness/declarative_checks.py`, extend `find_check_specs(repo)` to
   `find_check_specs(repo, state_dir="")`: after loading repo specs, also
   load `{state_dir}/checks/*.json` when the directory exists, appending
   those specs AFTER repo specs (state-dir specs are additive, not
   replacing). `declarative_checks_enabled` gains the same optional
   `state_dir` param: enabled when EITHER directory exists (kill switch env
   still wins).
2. Update both call sites in `harness/worker.py` to pass
   `base_cfg.state_dir` — note the pre-check site builds `base_cfg` AFTER
   the check today; hoist the `HarnessConfig.from_env()` call above the
   pre-check block so both sites share it.
3. Tests: state-dir-only specs run when the repo has none; repo + state-dir
   specs both run; kill switch disables both.
4. Commit: "Support session-level declarative check specs from the state dir"

## Task D: release prep (bump, no tag)

1. Find the version string locations from the v0.7.44 bump: run
   `git show a062833 --stat` and mirror every file it touched
   (expect `pyproject.toml` and `webapp/package.json`; follow whatever the
   bump commit actually changed). Bump to `0.7.45`.
2. Full verification first: `python -m pytest -q` green, `npm run build`
   green.
3. Commit: "chore(release): bump version to 0.7.45" and push to main with
   the other commits.
4. Wait for CI green (pytest 3.9 + 3.11 + frontend-build), e.g.
   `gh run watch <id> --exit-status`. Report the run id and status.
5. Do NOT push a tag. The `v0.7.45` tag is cut by the user after this plan's
   work is reviewed.

## Verification (after ALL tasks)

```powershell
cd C:\Users\pwall\Projects\marionette
.\.venv\Scripts\python.exe -m pytest -q
cd webapp; npm run build; cd ..
git push origin main
# gh run watch <run-id> --exit-status  (all three legs green)
# NO TAG.
```

## Out of scope (do not do)

- RQGM evaluator lifts (evaluator slots, epoch freezing, anchor sets) —
  next wave, Puppetmaster repo, design-first.
- Puppetmaster-side check enforcement or VERIFICATION artifact mirroring
  (the worker-summary surfacing in Task B is this round's UI story).
- OMP medium-value leftovers (time-travel rules, advisor, AST preview,
  persistent eval).
- YAML support, inline `checks:` blocks on swarm payloads.
- Pushing any tag.
