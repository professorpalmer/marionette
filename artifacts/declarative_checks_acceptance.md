# Declarative pre/post checks — acceptance notes

## Scope

Shepherd-style declarative checks for harness worker tasks. v1 covers ``shell``,
``file``, and post-only ``artifact`` kinds (Puppetmaster store queries by job id).

## Behavior

- **Specs:** JSON files in ``{repo}/.marionette/checks/*.json`` with ``pre`` and
  ``post`` arrays. Kill switch: ``HARNESS_DECLARATIVE_CHECKS=0``.
- **Runner:** ``harness/declarative_checks.py`` — ``load_checks``,
  ``find_check_specs``, ``run_checks`` (never raises; 4000-char output cap).
- **Enforcement:** ``ProviderWorker`` in ``harness/worker.py`` runs ``pre``
  checks on the worktree before ``run_auto``; ``on_fail=blocked`` aborts the
  worker. ``post`` checks run after the turn (including ``artifact`` checks that
  query the Puppetmaster store for the worker's ``job_id``); ``on_fail=failed``
  marks the worker result failed. Results attach to ``WorkerResult.declarative_checks``.
- **Parse errors:** malformed spec files produce ``warn`` results via
  ``discover_check_parse_warnings``; they never crash the worker.

## Tests

```bash
python -m pytest tests/test_declarative_checks.py -q
```

## Non-goals honored

- No YAML support or Puppetmaster-side enforcement beyond artifact store reads.
- Interactive pilot ``harness/verify.py`` auto-verify unchanged.
- No UI spec editor.
