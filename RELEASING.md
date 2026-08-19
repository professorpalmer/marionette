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
Green CI Before Tag still holds: the `tests` workflow (3.9 floor, 3.11,
Windows, frontend-build) must be green for **this git tree**. The tag may
point at the dest-into-main merge commit; it does not need a second `tests`
run on that SHA when `merge^{tree}` equals the already-green dest PR tree.

`.github/workflows/release.yml` does **not** re-run pytest. On a dest-into-main
PR it starts installer builds in parallel with `tests.yml`. On a `v*` tag it
checks that a successful `tests` run exists for this tree (or this commit),
reuses those PR installers when the tree matches, and publishes only after
that check. Users wait `max(tests, builds)`, not tests + builds + a second
pytest gate.

If a conflict resolution changes the tree, wait for `tests` on the new tree.
That is the only exception.

```bash
# Preferred ship path (version bump already on dest, dest contains main):
# 1. Open dest -> main. Wait for that PR's tests matrix.
# 2. Merge. Confirm merge^{tree} == dest^{tree}.
# 3. Tag immediately:
git tag vX.Y.Z
git push origin vX.Y.Z

# Or the source-run helper (refuses to tag a red/unknown tree):
bash scripts/release.sh X.Y.Z "release notes"
```

`scripts/release.sh` bumps `webapp/package.json`, commits `release: vX.Y.Z` if
needed, requires a green `tests` workflow for the resulting tree, then tags
and pushes. A version bump on `main` that was not in the dest PR changes the
tree; wait for `tests` on that commit.

### Faster Windows (optional)

`tests.yml` reads `vars.CI_WINDOWS_RUNNER` and falls back to `windows-latest`.
After the Blacksmith GitHub App is installed on this repo, set:

```bash
gh variable set CI_WINDOWS_RUNNER --body blacksmith-4vcpu-windows-2025 \
  --repo professorpalmer/marionette
```

Same YAML, different machines. Do not hardcode the Blacksmith label until the
app is installed or Windows jobs queue forever.

## Diverged / self-edited checkouts

Because Marionette can edit its own source, a checkout may be dirty or ahead of
`origin/main`. Fast-forward-only update refuses to rewrite local work; the update
UI surfaces the diverged-tree options (stash + apply, update onto a branch, or
reset) instead of failing silently. See `update-bridge.cjs` for the handling.
