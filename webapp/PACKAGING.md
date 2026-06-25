# Packaging PM Harness (macOS)

The desktop app is an Electron shell (webapp/) that spawns the Python backend
(harness.cli gui) and talks to it over a localhost loopback + auth token.

## Build a self-contained portable installer

To build the self-contained desktop installer with a custom app icon (which includes the bundled Python backend with no runtime dependency on a local repository or virtual environment), run:

```bash
cd webapp
npm run dist:full      # -> webapp/release/PM Harness-0.1.0-arm64.dmg
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

## Code Signing and Notarization

To distribute PM Harness to other macOS machines without Gatekeeper friction, you need an Apple Developer certificate ($99/yr). The build configuration is fully wired for signing and notarization, but gates these processes on the presence of corresponding environment variables. If these variables are not present, the build cleanly skips signing and notarization, allowing unsigned personal or development builds to function with no extra configuration.

### Environment Variables for Distribution
Once you obtain an Apple Developer Certificate, export the following environment variables prior to running the packaging command:

- CSC_LINK: Path to your exported Developer ID Application Certificate file (.p12) or its base64-encoded content.
- CSC_KEY_PASSWORD: The password you set when exporting the .p12 certificate.
- APPLE_ID: Your Apple Developer Account email address (Apple ID).
- APPLE_APP_SPECIFIC_PASSWORD: An app-specific password generated via your Apple ID account portal (not your main Apple ID password).
- APPLE_TEAM_ID: Your 10-character Apple Developer Team ID.

### The Packaging Command
With the environment variables set, you can build a signed and notarized DMG by running:

```bash
cd webapp
npm run dist:dmg
```

This single command will:
1. Compile and build the React frontend.
2. Package the Electron shell for macOS, embedding the backend binary.
3. Automatically sign the entire application bundle, including the nested PyInstaller-bundled Python backend binary in the app's Resources/ directory. The entitlements configuration (allow-jit, allow-unsigned-executable-memory, and disable-library-validation) will be embedded during the signing process to ensure the hardened runtime allows executing the embedded PyInstaller-bundled Python backend.
4. Notarize the signed .app using @electron/notarize via the afterSign hook (webapp/build/notarize.cjs).
5. Package the notarized application inside a distributable DMG image.

### Unsigned Development Build Flow
If you do not have a certificate, the build skips signing and notarization cleanly. To explicitly disable keychain scanning and enforce an unsigned build, use:

```bash
cd webapp
CSC_IDENTITY_AUTO_DISCOVERY=false npm run dist:mac
```

---

## Universal Binary Support & Investigation

As part of our commitment to platform independence, we investigated the feasibility of building a universal (arm64 + x86_64) macOS binary for PM Harness.

### Investigation Findings
1. Python Interpreter Analysis:
   - The project's active virtualenv Python interpreter (`~/pm-harness/.venv/bin/python`) is an arm64-only Mach-O executable.
   - Run verification outputs:
     `file ~/pm-harness/.venv/bin/python` -> `Mach-O 64-bit executable arm64`
     `lipo -archs ~/pm-harness/.venv/bin/python` -> `arm64`
2. PyInstaller Limitations:
   - PyInstaller bundles the backend binary by collecting the currently active Python interpreter and its dynamically loaded compiled libraries (.so / .dylib files).
   - Because PyInstaller uses the active interpreter, it can only produce a universal2 (arm64 + x86_64) backend binary if the Python interpreter itself is a universal2 binary containing both architectures.
3. Verdict:
   - Creating a universal build of the entire PM Harness package is currently blocked because the underlying Python virtual environment interpreter is arm64-only.
   - While Electron natively supports packaging universal macOS applications (by fetching both arm64 and x64 runtimes), a universal Electron .app with an arm64-only embedded backend binary would crash on Intel-based Macs. Therefore, it is an all-or-nothing requirement. We keep the build target strictly `arm64` for now to prevent shipping broken binaries to Intel users.

### Real Path to Universal Binaries
To build a fully functional universal2 package in the future, follow these steps:
1. Install a universal2 Python interpreter (3.9+) on your build machine (e.g., by downloading the macOS universal2 installer from python.org).
2. Re-create the virtual environment using the universal2 Python interpreter.
3. Configure the backend build script (scripts/build_backend.sh) and PyInstaller spec (build/pmharness-backend.spec) to target `universal2` (e.g., passing `--target-arch universal2` to PyInstaller).
4. Update the `electron-builder` configuration in webapp/package.json's `mac` section to change `target` to include `universal` (or `arm64` and `x64` targets) instead of just `arm64`.
5. Verify both architectures are present in the resulting backend binary using `lipo -archs webapp/backend-dist/pmharness-backend`.

## App Icon Generation

PM Harness uses a custom app icon. The source 1024x1024 PNG image is located at `webapp/build/assets/icon-source.png`.
To compile this image into a macOS `.icns` file, run the helper script from the repository root:

```bash
./scripts/make_icon.sh
```

This script creates a temporary `.iconset` directory, uses `sips` to resize the icon to the required standard resolutions (16x16, 32x32, 64x64, 128x128, 256x256, 512x512, and their `@2x` retina equivalents up to 1024x1024), compiles it into `webapp/build/icon.icns` using `iconutil`, and cleans up the temporary files.

## What the build produces

- release/PM Harness-0.1.0-arm64.dmg (The macOS disk image installer containing the `.app` package)
- release/mac-arm64/PM Harness.app (The unpackaged application bundle containing the Electron runtime, React frontend, and the embedded PyInstaller backend)
- Verified: launches, spawns the embedded backend, serves /api/config|skills|mcp (200), single shared backend per machine (marker reuse), auth token enforced.
