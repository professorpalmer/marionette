# Contributing to Marionette

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

Easiest path -- the source installer (uv-based, provisions Python + venv + node
deps + a `marionette` launcher):

```bash
curl -fsSL https://raw.githubusercontent.com/professorpalmer/marionette/main/scripts/install.sh | bash
```

Or set up a checkout by hand. Backend (uv provides Python per `.python-version`):

```bash
uv venv .venv
uv pip install --python .venv -e . "puppetmaster-ai==1.20.8"
.venv/bin/python -m pytest -q          # full offline suite -- must be green
```

Frontend + app (dev mode with hot-reload):

```bash
cd webapp && npm install
bash scripts/dev.sh                    # Vite HMR + source backend (marionette dev)
```

Editing a `.tsx` hot-reloads instantly; editing a `.py` is picked up on the next
backend spawn (Cmd+R the window). There is no packaging step -- Marionette always
runs from source.

## Workflow

1. Branch off `main`: `git checkout -b fix/<short-name>`.
2. Make the change. Add/adjust tests -- behavioral changes need a test.
3. Run `.venv/bin/python -m pytest -q` and `cd webapp && npm run build` locally.
   When touching full-auto / command policy / SSE reattach / tool-pair repair,
   also run the Wave 6 offline safety gate:
   `.venv/bin/python -m pytest -q -m full_auto_safety`
   (AutoBudget, command policy/approvals, tool-pair sanitizer, SSE ring-miss,
   stub deterministic eval â€” no live keys).
4. Open a PR against `main`. CI runs the same pytest matrix + frontend build +
   the `full-auto-safety` marker job and must pass before merge.
5. Keep commits scoped: don't fold unrelated work into one commit. A release
   commit is its own commit.

## How updates reach users (contributing IS the release)

Marionette self-updates from git, Hermes-style: every source checkout tracks
`main` and shows an `update (N)` pill when it's behind, then pulls + rebuilds +
relaunches in place. So **merging a green PR to `main` ships your change to every
source install** on their next relaunch. Tagged releases also rebuild the thin
Electron installers (macOS, Windows, Linux) via CI; those users pick up changes
after bootstrap + update or by installing a newer Release.

Keep `main` releasable: it must build (`npm run build`) and pass CI, because a
red `main` is what everyone's checkout tries to pull. The updater fast-forwards
only, so never force-push `main`.

## Releases

See `RELEASING.md`. Distribution is primarily the git self-update above; cutting
a version tag (`scripts/release.sh X.Y.Z`) also triggers CI to build and attach
the platform Electron installers to the GitHub Release.

Toolchain note: CodeGraph's native module needs a C/C++ compiler -- Xcode Command
Line Tools on macOS, `build-essential` on Linux, Visual Studio Build Tools on
Windows.
