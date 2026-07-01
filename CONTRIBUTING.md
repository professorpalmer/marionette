# Contributing to Marionette (pm-harness)

Welcome. This is an internal-first research rig + Electron desktop app. These are
the conventions that keep the codebase coherent -- please follow them.

## Ground rules (non-negotiable)

- **No emojis or decorative pictographs anywhere** -- code, UI strings, commit
  messages, docs, output. Plain words only ("copied", not "copied [check]").
  Typographic characters (em-dash, arrows in prose) are fine.
- **stdlib-only for the rig itself** (urllib, sqlite, dataclasses). Puppetmaster
  (`puppetmaster-ai` on PyPI) is the single real runtime dependency.
- **`pmharness/intent.py` stays PM-free and pure** so it unit-tests fast and
  hermetically. Execution coupling lives only in `bridge.py`.
- **Scoring is deterministic** -- no LLM-as-judge. Every metric is a function of
  (labeled task, raw driver text, execution result).
- **Tests before claiming done.** The offline suite must stay green with zero API
  keys. Never commit keys or `results/*.sqlite`.

## Local setup

Backend (Python, 3.9+):

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]" puppetmaster-ai
.venv/bin/python -m pytest -q          # full offline suite -- must be green
```

Frontend + app (dev mode with hot-reload -- NO DMG rebuild needed):

```bash
cd webapp && npm install
bash scripts/dev.sh                    # Vite HMR + source backend
```

Editing a `.tsx` hot-reloads instantly; editing a `.py` is picked up on the next
backend spawn (Cmd+R the window). You only build a DMG to ship a release -- never
to test your own changes.

## Workflow

1. Branch off `main`: `git checkout -b fix/<short-name>`.
2. Make the change. Add/adjust tests -- behavioral changes need a test.
3. Run `.venv/bin/python -m pytest -q` and `cd webapp && npm run build` locally.
4. Open a PR against `main`. CI runs the same pytest matrix + frontend build and
   must pass before merge.
5. Keep commits scoped: don't fold unrelated work into one commit. A release
   commit is its own commit.

## How updates reach users (contributing IS the release)

Marionette self-updates from git, Hermes-style: the installed app tracks `main`
and every running instance shows an `update (N)` pill when it's behind, then
pulls + rebuilds + relaunches in place. So **merging a green PR to `main` ships
your change to the whole circle** on their next relaunch -- no DMG hand-off.

Keep `main` releasable: it must build (`npm run build`) and pass CI, because a
red `main` is what everyone's app tries to pull. The updater fast-forwards only,
so never force-push `main`.

## Releases

See `RELEASING.md`. The primary channel is the git self-update above. A signed +
notarized DMG (`scripts/release.sh X.Y.Z`) still exists for non-dev testers who
don't keep a checkout; don't hand-edit release artifacts.
