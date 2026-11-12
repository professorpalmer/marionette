# Install and update

Marionette runs from source, the way Hermes does. One installer works on
macOS, Linux, and Windows. It clones the repo, builds a per-machine Python
venv with `uv`, installs the node deps, builds the renderer, and puts a
`marionette` launcher on your PATH. Native modules compile on your machine.

## Install

macOS / Linux:

```bash
curl -fsSL https://professorpalmer.github.io/marionette/install.sh | bash
```

Windows (PowerShell):

```powershell
irm https://professorpalmer.github.io/marionette/install.ps1 | iex
```

Fallback (raw GitHub URLs):

```bash
curl -fsSL https://raw.githubusercontent.com/professorpalmer/marionette/main/scripts/install.sh | bash
```

```powershell
irm https://raw.githubusercontent.com/professorpalmer/marionette/main/scripts/install.ps1 | iex
```

## Launcher commands

```bash
marionette            # daily use (built renderer, in-app updates)
marionette dev        # contributor hot-reload (Vite HMR)
marionette doctor     # re-check the environment
marionette update     # git pull + rebuild
```

## Requirements

- `git`
- A C/C++ toolchain for the CodeGraph native module: Xcode Command Line
  Tools on macOS, `build-essential` on Linux, Visual Studio Build Tools on
  Windows.
- Node >= 20 (`scripts/doctor.sh` fails on an older major).
- `uv` provides Python. `marionette doctor` checks the whole environment.

## Two distribution paths, one app

**Path 1: source installer.** Recommended for contributors and power users.
The curl / irm installers above clone the repo, build a per-machine venv,
and put a `marionette` launcher on your PATH. Updates happen in the app:
the status-bar `update` pill runs `git pull` + rebuild + relaunch. A merge
to `main` reaches every source checkout on the next click. Marionette can
edit its own source, so the updater stashes and reapplies local self-edits.
It flags a diverged fork. It does not fail silently.

**Path 2: thin Electron shell.** Optional. GitHub Releases also ship
installers: DMG and zip on macOS, `.exe` on Windows, AppImage on Linux. The
packaged app is a thin shell. On first launch it clones and bootstraps the
same source tree and venv, then runs the same backend and renderer. You get
a normal desktop installer without a bundled Python runtime.

Both paths run from the same checkout after bootstrap. A version tag labels
what you are on. See [Releasing](../develop/releasing.md).

## Run from a checkout (contributors)

Desktop app with Vite hot reload:

```bash
cd webapp && npm install && npm run electron:dev
```

Or the one-command dev launcher. It cleans stale processes, then launches:

```bash
bash scripts/dev.sh          # same as: marionette dev
```

The full local setup and the PR workflow are in
[CONTRIBUTING.md](../../CONTRIBUTING.md).

## Research rig (offline, no keys)

```bash
python3 -m venv .venv && .venv/bin/pip install -e /path/to/Puppetmaster pytest
.venv/bin/python -m pytest -q                              # full suite, offline
.venv/bin/python -m pytest -q -m full_auto_safety          # Wave 6 offline safety gate
.venv/bin/python scripts/run_eval.py --drivers stub-oracle # offline oracle ceiling
```

The rig modules are described in [`pmharness/`](../../pmharness/README.md).
