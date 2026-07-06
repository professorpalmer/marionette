# Design: declarative pre/post checks for worker tasks

## Problem

Marionette today runs **reactive** verification: `harness/verify.py` infers a fast
project check after edits (`detect_verify_command`, `build_scoped_command`,
`run_verify`) and Puppetmaster workers emit unstructured `VERIFICATION` artifacts
when adapters finish. Neither path lets an operator declare **what must hold**
before or after a task (file exists, grep match, exit code, artifact field) in a
portable, machine-readable spec. Shepherd-style checks would close the loop:
workers fail fast with a typed phase instead of prose-only verification blobs.

## Proposed shape

**Check spec (YAML or JSON, one file per task or per role template):**

```yaml
version: 1
pre:
  - id: tree-clean
    kind: shell
    cmd: git diff --quiet
    on_fail: blocked
post:
  - id: tests-green
    kind: shell
    cmd: python -m pytest tests/test_foo.py -q
    timeout_s: 120
    on_fail: failed
  - id: patch-present
    kind: artifact
    expect:
      type: PATCH
      min_count: 1
```

**Kinds (v1):** `shell` (subprocess, cwd=repo), `artifact` (query Puppetmaster
store by job/task), `file` (exists / contains / not_contains). Each check returns
`{id, phase, passed, output, duration_ms}`.

**Failure phase taxonomy:** `blocked` (pre-check; worker never starts),
`failed` (post-check; job terminal failure), `warn` (recorded, job may succeed).

**Where specs live:** `{repo}/.marionette/checks/` for repo defaults;
optional inline `checks:` block on swarm worker payload (Puppetmaster task spec);
session-level overrides in `{state_dir}/checks/` for harness-only runs.

## Integration points

| Layer | Hook |
|-------|------|
| Enforcement (pre) | `harness/worker.py` — before `ProviderWorker` dispatches tools, run pre checks; abort with structured event if `blocked`. |
| Enforcement (post) | Same module after worker turn completes; merge results into job completion path already used by `harness/conversation.py` auto-verify (`run_verify` pattern: never raise). |
| Runner | New `harness/declarative_checks.py`: `load_checks(spec_path \| dict)`, `run_checks(checks, ctx)`, reusing `verify.py` subprocess discipline (`MAX_OUTPUT`, timeout, cancel_event). |
| Artifact queries | `InternalUriContext.store()` / `create_store` (same seam as `harness/internal_uri.py`) to assert `artifact://job_id/...` expectations. |
| Puppetmaster | Workers persist a `VERIFICATION` artifact with `payload.checks[]` mirroring Marionette shape so SwarmPane and `artifact://` reads stay unified. |
| UI | SwarmPane / job detail: surface failed check id + truncated output (no new bespoke schema). |

## Test strategy

- Unit: `tests/test_declarative_checks.py` — each kind with tmp repo + fake store;
  timeout and `on_fail` phase mapping; loader rejects traversal in paths.
- Integration: extend `tests/test_harness_e2e.py` pattern with a stub worker
  carrying a one-line post `file` check; assert job completes/fails deterministically.
- Regression: existing `tests/test_verify.py` (if present) and auto-verify path
  unchanged when no declarative spec is configured.

## Non-goals (this tranche)

- Replacing `harness/verify.py` heuristic auto-verify for interactive pilot turns.
- Long-running CI suites as default post checks (keep sub-10s scoped commands).
- Mutating checks (deploy, push, apply patch) — read-only assertions only.
- UI spec editor; file-based specs only at first ship.
- Cross-repo check inheritance or a global check marketplace.
