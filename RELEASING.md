# Releasing Marionette

Marionette runs from a source checkout -- there is no packaged `.app`, no DMG,
and no notarization. Every install is a git clone with a per-machine `.venv` and
a locally built renderer (the Hermes model). A "release" therefore just means
"the tracked branch moved forward"; users pick it up with the in-app
**Update & Relaunch** pill.

## How an update reaches everyone

The status-bar update pill runs the in-place source updater
(`webapp/electron/update-bridge.cjs`, pure helpers in `update-*.cjs`, unit tested
via `npm run test:electron`):

- `git fetch` + fast-forward the tracked branch tip,
- `uv pip install -e .` **only if** a Python dep file changed,
- `npm ci` **only if** `webapp/package-lock.json` changed,
- `npm run build` (retry once) to rebuild the renderer,
- relaunch (backend torn down first so it comes back on the new code).

So merging to `main` is the entire distribution mechanism. There is nothing to
sign, upload, or per-arch build: Intel + Apple Silicon Macs and Linux all run the
same source, and native modules (`better-sqlite3`) compile locally at install.

## Cutting a version tag (optional, cosmetic)

Tags/versions exist only so `app.getVersion()` and the update pill show a
human-readable version -- they are not required for delivery.

```bash
bash scripts/release.sh X.Y.Z "release notes"
```

This bumps `webapp/package.json`, commits `release: vX.Y.Z`, tags it, pushes
`main` + the tag, and cuts a notes-only GitHub Release (no attached binary).

## Diverged / self-edited checkouts

Because Marionette can edit its own source, a checkout may be dirty or ahead of
`origin/main`. Fast-forward-only update refuses to rewrite local work; the update
UI surfaces the diverged-tree options (stash + apply, update onto a branch, or
reset) instead of failing silently. See `update-bridge.cjs` for the handling.
