# Round 9 implementation plan (wrap-up: parity sweep, flag docs, housekeeping)

Instructions for the implementing model. All work happens in
`C:\Users\pwall\Projects\marionette`. Work the tasks IN ORDER. One commit
per task with the exact message given. Run the full suite
(`python -m pytest tests -q`) before EVERY commit, and `npm run build` in
`webapp/` before the final commit. Commit locally only: do NOT push, do
NOT tag, do NOT run `gh` — the user pushes and cuts the release after
review.

This is a WRAP-UP round. Do not add features. Do not refactor beyond what
a task explicitly asks. If a task's instructions and the code disagree,
prefer the smallest change that satisfies the instruction, and note the
discrepancy in the commit body (one line).

Ground rules (same as rounds 4-8):

- No emojis anywhere. Python 3.9 floor: no `match` statements, no
  `X | Y` union syntax in runtime annotations;
  `from __future__ import annotations` at the top of any module you edit
  that does not already have it.
- JSON, never YAML. No new dependencies.
- Windows/macOS parity is the POINT of this round: `os.path.join`, UTF-8
  with `errors="replace"` on reads, no shell-specific commands in Python
  code.
- PowerShell (your shell): `;` separates commands, never `&&`.

## Task A: cross-platform parity sweep

Goal: find and fix latent Windows/macOS hazards in `harness/`. Marionette
has many Mac users and is developed on Windows; both must behave 1:1.

Run each check below over `harness/` ONLY (not `tests/`, not `webapp/`).
For each hit, apply the standard fix; if a hit is a false positive
(e.g. inside a docstring, or genuinely platform-guarded), leave it and
list it in the commit body as "reviewed, safe".

1. Unencoded file opens. Find text-mode `open(` calls missing an
   `encoding=` argument:
   `python -c "import re,pathlib; [print(p, i+1, l.strip()) for p in pathlib.Path('harness').glob('*.py') for i,l in enumerate(p.read_text(encoding='utf-8').splitlines()) if re.search(r'open\(', l) and 'encoding=' not in l and 'rb' not in l and 'wb' not in l]"`
   Fix: add `encoding="utf-8"` (reads also get `errors="replace"` when
   the data may be user/tool output rather than our own JSON).
   Binary-mode opens (`"rb"`/`"wb"`) are exempt.
2. Hardcoded path separators. Search `harness/` for string literals
   containing `/tmp`, `\\` used as a path separator in non-Windows-only
   code, and `.replace("\\", "/")`-style normalization. Fix: `tempfile`
   APIs for temp paths, `os.path.join` for joins. URI-scheme strings
   (`state://`, `session://`, etc.) and regexes are exempt.
3. POSIX-only process APIs. Search for `os.kill`, `signal.SIGKILL`,
   `signal.SIGTERM`, `os.setsid`, `preexec_fn`. Any hit must either be
   inside an explicit `os.name != "nt"` / `sys.platform` guard with a
   Windows branch, or be fixed to have one.
4. Subprocess text capture. Every `subprocess.run`/`Popen` with
   `capture_output=True` or `stdout=PIPE` and `text=True` must also set
   `encoding="utf-8"` and `errors="replace"` (Windows defaults to cp1252
   and dies on emoji in tool output). Fix in place.
5. Shell-specific commands. Search for `shell=True` combined with unix
   idioms (`&&`, `|`, `grep`, `rm -rf`, backticks). Any hit must be
   rewritten as an argument list without `shell=True`, or explicitly
   platform-guarded.
6. Case-sensitivity traps. Search for `.lower()` applied to file paths
   used for equality/membership checks against unlowered paths. Fix by
   comparing `os.path.normcase(os.path.abspath(...))` on both sides.

For every code change, add or extend a test in the matching
`tests/test_*.py` file when a behavior is testable cross-platform (e.g.
encoding fixes are testable by writing a file containing a non-ASCII
character and reading it back through the changed code path). Pure
hygiene changes with no observable behavior difference do not need new
tests.

Commit: "Cross-platform parity sweep over harness"

## Task B: feature flag reference doc

Goal: one authoritative table of every `HARNESS_*` environment toggle.

1. Enumerate the toggles mechanically:
   `python -c "import re,pathlib; names=set(); [names.update(re.findall(r'HARNESS_[A-Z0-9_]+', p.read_text(encoding='utf-8'))) for p in pathlib.Path('harness').glob('*.py')]; [print(n) for n in sorted(names)]"`
2. Write `artifacts/feature_flags.md`: a markdown table with columns
   Flag, Default, Effect, Introduced-in (round/version if determinable
   from `git log -S <FLAG> --oneline -- harness/`, otherwise "-").
   For each flag, read the code that consumes it to state the REAL
   default (on/off) and one-sentence effect. Every flag found in step 1
   must appear in the table; do not invent flags that are not in code.
3. Cross-check: for any flag whose code default contradicts its design
   note in `artifacts/design_*.md`, add a "Notes" line under the table
   naming the discrepancy. Do not change code defaults in this task.
4. Commit: "Add feature flag reference for all HARNESS env toggles"

## Task C: track the accumulated plan documents

Goal: the untracked round plans become part of the repo history.

1. `git add` exactly these files (and nothing else):
   `artifacts/omp_round7_plan.md`, `artifacts/omp_round8_plan.md`,
   `artifacts/omp_round9_plan.md`, `artifacts/rqgm_round7_plan.md`,
   `artifacts/rqgm_round8_plan.md`, `artifacts/rqgm_round9_plan.md`,
   `artifacts/rqgm_round10_plan.md`.
2. Verify `git status --short` shows no remaining untracked files under
   `artifacts/`. If other untracked files exist elsewhere, leave them.
3. Commit: "Track round 7-10 implementation plans"

## Task D: version bump (local commit, NO push)

1. Bump to `0.7.49` in `pyproject.toml`, `harness/__init__.py`,
   `webapp/package.json`, and `webapp/package-lock.json` (both version
   fields in the lock).
2. `python -m pytest tests -q` — everything green. `npm run build` in
   `webapp/` — builds clean.
3. Commit: "chore(release): bump version to 0.7.49". Do NOT push, do NOT
   tag, do NOT run `gh`.

## Verification (after ALL tasks)

```powershell
cd C:\Users\pwall\Projects\marionette
python -m pytest tests -q
cd webapp; npm run build; cd ..
git log --oneline -5
git status --short
# four new local commits; no untracked artifacts/*.md remain.
# NO PUSH. NO TAG.
```

## Out of scope (do not do)

- New features, new tools, new endpoints, new UI.
- Puppetmaster repo changes.
- Changing any feature flag's default value.
- Refactors not required by a specific Task A finding.
- Pushing anything to origin.

## After Round 9

The user reviews, pushes, waits for CI green on all legs (including the
3.9 floor and frontend-build), and cuts the Marionette release covering
0.7.46 through 0.7.49. This closes the OMP arc.
