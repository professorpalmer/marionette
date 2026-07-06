# Round 3 implementation plan (post-OMP research lifts)

Instructions for the implementing model. Work the tasks IN ORDER. One commit
per task with the exact message given. Run the full test suite before every
commit; run `npm run build` in `webapp/` additionally whenever a task touched
frontend files. Push to main at the end and wait for CI green. Do NOT create
any release tag.

Read the referenced design note fully before starting each task — the notes
are the contract; this plan only adds sequencing and guardrails.

Ground rules (same as rounds 1-2):

- No emojis anywhere. stdlib-only for the rig (`urllib`, `sqlite3`,
  `dataclasses`, `subprocess`).
- SQLite writers open, write, and CLOSE the connection on every call
  (Windows file-lock: a held handle breaks TemporaryDirectory cleanup with
  WinError 32). Copy the discipline in `harness/spill_registry.py`.
- Anything that hooks a hot path must swallow its own failures
  (try/except around the write, never around the caller's logic).
- Scoring/measurement stays deterministic. No LLM-as-judge.
- `pmharness/intent.py` stays PM-free and pure.

Context: Round 2 shipped tasks A-F (`artifacts/omp_round2_plan.md`), and the
memory-offload design has ALREADY BEEN IMPLEMENTED (`spill://` URIs; see
`artifacts/spill_offload_acceptance.md`). Do not re-implement it. The two
remaining design notes are the meat of this round.

## Task A: declarative pre/post checks v1

Design contract: `artifacts/design_declarative_checks.md`. Read it first.
Scope v1 to the harness worker path only — no Puppetmaster-side changes.

1. New module `harness/declarative_checks.py` (stdlib only):
   - `@dataclass(frozen=True) CheckSpec`: `id: str`, `kind: str` (one of
     `shell`, `file`), `phase: str` (`pre` or `post`), plus kind-specific
     fields: `cmd`/`timeout_s` for shell; `path`/`exists`/`contains`/
     `not_contains` for file. NOTE: v1 drops the `artifact` kind from the
     design — it needs the Puppetmaster store and is deferred (record this
     in the design note's non-goals when you finish).
   - `@dataclass CheckResult`: `id`, `phase`, `passed: bool`, `output: str`
     (truncated to 4000 chars, same cap as `harness/verify.py` MAX_OUTPUT),
     `duration_ms: int`.
   - `load_checks(source) -> list[CheckSpec]` where source is a dict (already
     parsed) or a JSON file path. Support JSON only in v1 (no YAML — the rig
     has no YAML dependency and must not gain one). Spec file shape follows
     the design note but as JSON. Validate: unknown `kind` or `on_fail`
     raises ValueError with the offending id; `file` paths must be
     repo-relative and reject `..` segments (mirror
     `_require_repo_relative_path` in `harness/internal_uri.py`).
   - `run_checks(checks, *, repo, phase, timeout_default=30, cancel_event=None)
     -> list[CheckResult]`. Shell checks reuse the subprocess discipline of
     `harness/verify.py::run_verify` (shell=True, cwd=repo, capture stdout+
     stderr, never raises, timeout -> failed result with note). File checks
     are pure os/path + read.
   - `on_fail` mapping per design: `blocked` (pre), `failed` (post), `warn`
     (either). `run_checks` just reports; enforcement is the caller's job.
2. Spec discovery: `find_check_specs(repo) -> list[CheckSpec]` reads every
   `*.json` in `{repo}/.marionette/checks/` (sorted by filename). Missing
   directory returns []. Malformed file: skip it and include a `warn` result
   explaining the parse failure — never crash the worker.
3. Enforcement point: `harness/worker.py`. Find where `ProviderWorker` starts
   its session/turn (read the file fully first). Before the first turn, run
   phase `pre`; if any check with `on_fail=blocked` failed, do not run the
   worker — emit the structured failure through whatever event/result path
   the worker already uses for errors (find how worker failures currently
   surface and reuse it; do not invent a new channel). After the worker's
   final turn, run phase `post` and attach the results to the worker result
   payload. Failed `on_fail=failed` post checks mark the worker result failed.
4. Config toggle: checks run whenever `{repo}/.marionette/checks/` exists.
   Env kill switch `HARNESS_DECLARATIVE_CHECKS=0` disables (default enabled;
   reading an absent directory is free).
5. Tests: new `tests/test_declarative_checks.py`:
   - load: valid spec round-trip; unknown kind rejected; traversal path
     rejected; malformed file yields warn result not exception.
   - run: shell check pass/fail/timeout (use `python -c` commands so they are
     cross-platform — NEVER bash-isms, this must pass on Windows);
     file exists/contains/not_contains.
   - enforcement: a worker-level test that a `blocked` pre check prevents the
     worker turn (mock/stub the provider like existing worker tests do — see
     how `tests/test_worker_deadline.py` builds a worker with
     `stub-oracle-v2`).
6. Acceptance doc: `artifacts/declarative_checks_acceptance.md` (mirror the
   shape of `artifacts/spill_offload_acceptance.md`).
7. Commit: "Add declarative pre/post checks for worker tasks"

## Task B: provider cassettes v1

Design contract: `artifacts/design_provider_cassettes.md`. Read it first.
Deviations allowed by this plan: use JSON (not YAML) for cassette files, for
the same no-new-dependency reason as Task A. One JSON file per cassette.

1. New `pmharness/drivers/cassette.py`:
   - `CassetteDriver` implementing the `Driver` protocol
     (`pmharness/drivers/base.py`): wraps an inner driver, `name` is
     `f"cassette({inner.name})"`.
   - Request hashing: canonical JSON of `(method, model, normalized_messages,
     tool_names)` -> sha256. Normalization: keep role+content only; replace
     `tool_call_id` values with their ordinal index; sort tool names. Put the
     normalizer in the same module (a separate module is overkill for v1).
   - Modes from `HARNESS_CASSETTE_MODE`: `record` (call inner, append
     interaction, save), `replay` (look up by hash; raise a clear KeyError
     naming the hash and cassette path when missing), unset/other = pure
     passthrough.
   - Cassette location: `HARNESS_CASSETTE_DIR` (required when mode is set;
     raise at construction if missing), file
     `{dir}/{sanitized_driver_name}.json`. File shape per the design note
     (`version`, `driver`, `recorded_at`, `interactions[]`).
   - Secret scrubbing: before writing, walk the stored request/response dicts
     and redact any string value that matches the loaded provider key(s) or
     `sk-[A-Za-z0-9_-]{8,}`. Store `scrubbed_fields` per interaction.
     Never write env var values into the file.
   - Replay returns a `DriverResponse` reconstructed from the stored fields
     (text, tokens_in, tokens_out, model) with `latency_ms=0.0`.
2. Wiring: find where the harness resolves its driver instance
   (`harness/config.py` or session bootstrap — search for how
   `cfg.driver` becomes an object). Wrap the resolved driver in
   `CassetteDriver` when `HARNESS_CASSETTE_MODE` is set. Keep the wrap at ONE
   choke point; do not sprinkle it.
3. Tests: new `tests/test_cassette_driver.py` (fully offline):
   - record with a stub inner driver into a tmp dir; replay twice; identical
     `DriverResponse` text/tokens both times; the inner driver is called
     exactly once total.
   - replay miss raises with the hash in the message.
   - scrubbing: plant a fake `sk-...` string in a recorded response; assert
     the file on disk does not contain it.
   - passthrough mode calls inner and writes nothing.
4. Acceptance doc: `artifacts/provider_cassettes_acceptance.md`.
5. Commit: "Add record/replay cassette layer for provider drivers"

## Task C: spill polish (retention + usage surfacing)

Finishes the two pieces the memory-offload ship deferred
(`artifacts/spill_offload_acceptance.md`, non-goals section).

1. Retention: in `harness/spill_registry.py` add
   `sweep_expired_spills(state_dir, retention_days) -> int` deleting rows AND
   their files older than the cutoff (file deletion best-effort, row deletion
   authoritative; never raises, returns rows removed). Call it once per
   session construction in `ConversationalSession.__init__` (find where other
   state-dir setup happens) guarded by env
   `HARNESS_SPILL_RETENTION_DAYS` — unset or `0` means keep forever (default).
2. Usage surfacing: add `spill_count` and `spill_chars` (sum) for the current
   session to `get_context_usage` in `harness/conversation.py` — follow
   `_history_compaction_fields()` exactly (sibling helper, merged into the
   same return dict, failure returns zeros). Surface both through
   `/api/usage` in `harness/server.py` the same way the history-compaction
   fields flow (search for `history_compactions` and mirror it).
   Frontend: extend the usage type in `webapp/src/lib/api.ts` and add one row
   "Offloaded outputs" in `webapp/src/components/CostBreakdown.tsx`, shown
   only when `spill_count > 0`, styled identically to its neighbors.
3. Housekeeping: `git mv results/TOOL_DISCOVERY_ACCEPTANCE.md
   artifacts/tool_discovery_acceptance.md` — round 1 left it untracked in
   `results/`, which is reserved for uncommittable run outputs.
4. Tests: extend `tests/test_spill_registry.py` (sweep removes old rows and
   files, keeps new; retention 0 keeps all) and mirror an existing
   `/api/usage` test for the two new fields (see how
   `tests/test_tool_output_savings.py` asserts usage fields).
5. This task touches the frontend: run `npm run build` in `webapp/` before
   committing.
6. Commit: "Add spill retention sweep and usage surfacing"

## Verification (after ALL tasks)

```powershell
cd C:\Users\pwall\Projects\marionette
.\.venv\Scripts\python.exe -m pytest -q          # must be fully green
cd webapp; npm run build; cd ..                   # must be green (Task C touched webapp)
git push origin main
# wait for the tests workflow: BOTH pytest legs (3.9 floor + 3.11) and
# frontend-build must pass. gh run watch <id> --exit-status
# Do NOT push any tag.
```

Python 3.9 floor reminders: no `match`, no `X | Y` type unions in runtime
annotations (`from __future__ import annotations` at module top is the
existing convention — keep it), no `zoneinfo`-dependent logic.

## Out of scope (do not do)

- The `artifact` check kind, YAML support, or Puppetmaster-side check
  enforcement (Task A defers all three).
- HTTP-level record/replay, cassette encryption, cassette merging (Task B).
- Cross-session spill dedupe or remote spill storage (Task C).
- Flipping any default: `HARNESS_HASH_EDIT` stays off,
  `HARNESS_CASSETTE_MODE` stays unset.
- Any release tag or version bump.
