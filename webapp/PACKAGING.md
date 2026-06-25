# Packaging PM Harness (macOS)

The desktop app is an Electron shell (webapp/) that spawns the Python backend
(harness.cli gui) and talks to it over a localhost loopback + auth token.

## Build a self-contained portable app

To build the self-contained app that includes the bundled Python backend (with no runtime dependency on a local repository or virtual environment), run:

```bash
cd webapp
npm run dist:full      # -> webapp/release/mac-arm64/PM Harness.app
```

This script:
1. Bundles the Python backend into a single self-contained executable (pmharness-backend) using PyInstaller, outputting to webapp/backend-dist/pmharness-backend.
2. Compiles/builds the React frontend.
3. Packages the Electron app, embedding the backend executable inside the app's Resources/ directory (so it is copied into Contents/Resources/pmharness-backend inside the .app package).

On launch, if the app is packaged and the bundled backend exists under process.resourcesPath, the Electron process spawns it directly. Otherwise, it falls back to the local development environment (.venv/bin/python).

Double-click PM Harness.app (or drag to /Applications). On first launch macOS
Gatekeeper will warn it is unsigned: right-click -> Open, or
`xattr -dr com.apple.quarantine "PM Harness.app"`.

## Development / Personal Build (using local venv)

If you only want to build the Electron frontend shell and let it spawn the backend from your local development .venv at ~/pm-harness, you can run:

```bash
cd webapp
npm run dist:mac       # -> webapp/release/mac-arm64/PM Harness.app
```

This retains the original behavior and does not build or bundle the Python backend via PyInstaller, which is faster for local shell testing.

## Code signing + notarization (required only for distribution)

An unsigned build runs locally. To distribute to other machines without Gatekeeper
friction you need an Apple Developer account ($99/yr):

```
# in package.json build.mac: set identity to your "Developer ID Application" cert
# then notarize with @electron/notarize (APPLE_ID, APPLE_APP_SPECIFIC_PASSWORD,
# APPLE_TEAM_ID) and staple.
```

Not configured here — the current target is dir/unsigned for personal use.

## What the build produces

- release/mac-arm64/PM Harness.app (Electron runtime + frontend + embedded PyInstaller backend)
- Verified: launches, spawns the embedded backend, serves /api/config|skills|mcp (200),
  single shared backend per machine (marker reuse), auth token enforced.
