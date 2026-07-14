# Releasing Marionette

Marionette has two distribution paths that converge on the same source checkout:

1. **Source installer** -- the curl/irm scripts clone the repo, build a
   per-machine `.venv`, and install a `marionette` launcher. This is the Hermes
   model: every checkout tracks `main` and self-updates in place.
2. **Thin Electron shell** -- optional signed installers (DMG, `.exe`, AppImage)
   published on GitHub Releases. The packaged app bootstraps the same clone +
   venv on first launch; it does not bundle Python or a frozen backend.

A "release" therefore means the tracked branch moved forward **and**, when a
version tag is pushed, CI builds and uploads the platform installers.

## How an update reaches everyone

### Source checkouts

The status-bar update pill runs the in-place source updater
(`webapp/electron/update-bridge.cjs`, pure helpers in `update-*.cjs`, unit tested
via `npm run test:electron`):

- `git fetch` + fast-forward the tracked branch tip,
- `uv pip install -e .` **only if** a Python dep file changed,
- `npm ci` **only if** `webapp/package-lock.json` changed,
- `npm run build` (retry once) to rebuild the renderer,
- relaunch (backend torn down first so it comes back on the new code).

Merging to `main` is the distribution mechanism for source installs. Native
modules (`better-sqlite3`) compile locally at install time on macOS (Intel +
Apple Silicon), Linux, and Windows.

### Packaged shell users

Users who installed from a GitHub Release get the same in-app update pill once
the shell has bootstrapped its checkout. Fresh installs download the latest
tagged installer from Releases.

## Cutting a version tag

Tags/versions label what checkouts and installers report via `app.getVersion()`.
Pushing a `v*` tag triggers `.github/workflows/release.yml`: the offline test
suite must pass, then macOS, Windows, and Linux Electron shells are built and
uploaded to the GitHub Release (DMG/zip, `.exe`, AppImage, blockmaps, and
`latest*.yml` auto-update metadata).

```bash
bash scripts/release.sh X.Y.Z "release notes"
```

This bumps `webapp/package.json`, commits `release: vX.Y.Z`, tags it, pushes
`main` + the tag, and creates/updates the GitHub Release notes. CI attaches the
platform binaries when the workflow completes.

## Diverged / self-edited checkouts

Because Marionette can edit its own source, a checkout may be dirty or ahead of
`origin/main`. Fast-forward-only update refuses to rewrite local work; the update
UI surfaces the diverged-tree options (stash + apply, update onto a branch, or
reset) instead of failing silently. See `update-bridge.cjs` for the handling.
