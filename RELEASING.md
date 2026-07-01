# Releasing Marionette

Marionette has two delivery channels. The first is the default for the
contributor circle; the second is for non-dev testers.

## 1. Git self-update (primary)

The installed app tracks the `main` branch of a git checkout and updates itself
in place -- the Hermes model. There is **no build step to "release"**: merging to
`main` is the release.

How it reaches everyone:

1. A change lands on `main` (your merge, or a merged PR -- CI must be green).
2. On each running app, the status bar polls the branch tip on launch and shows
   an `update (N)` pill when the checkout is `N` commits behind.
3. Clicking it runs, in the checkout, streaming progress to the UI:
   - `git fetch` + `git merge --ff-only` the branch tip,
   - `pip install -e .` **only if** a Python dep file changed,
   - `npm ci` **only if** `webapp/package-lock.json` changed,
   - `npm run build` (retry once) to rebuild the renderer,
   - relaunch (backend torn down first so it comes back on the new code).

Implementation lives in `webapp/electron/update-*.cjs` (pure helpers, unit
tested via `npm run test:electron`) and `update-bridge.cjs` (the orchestrator).
The pattern is adapted with attribution from the Hermes Agent desktop updater
(MIT, Nous Research).

Fast-forward only: if a user has local commits or uncommitted changes on the
branch, the update stops with a clear message instead of rewriting their tree.

### Cutting a "version" (optional)

Version is cosmetic under this model (it labels the build in the status bar and
about box). To bump it, edit `webapp/package.json` `version` and merge. No tag
or artifact is required for self-update to work.

## 2. Notarized DMG (secondary -- non-dev testers)

For someone who wants a double-clickable app and no checkout, build a signed +
notarized DMG and attach it to a GitHub Release:

```bash
bash scripts/release.sh X.Y.Z "release notes"
```

Requires Apple notarization creds in the environment (`APPLE_ID`,
`APPLE_TEAM_ID`, `APPLE_APP_SPECIFIC_PASSWORD`, and the signing cert). See
`webapp/PACKAGING.md`. This path is a full app reinstall, not self-update; a DMG
user updates by downloading the next DMG. Prefer channel 1 for the contributor
circle.
