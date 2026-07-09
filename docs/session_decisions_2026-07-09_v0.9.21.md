# Session decisions — 2026-07-09 (v0.9.21)

## Google OAuth in-app browser on Windows

- Symptom: YouTube/Google sign-in in the Browser pane → "This browser or app may not be secure" on Windows; Mac was fine.
- Cause: hardcoded Chrome/132 UA while Electron 33 ships Chromium ~130 (Client Hints mismatch), plus Electron's AutomationControlled blink feature setting `navigator.webdriver`.
- Fix: UA from `process.versions.chrome` + platform token; `disable-blink-features=AutomationControlled`; trusted `browser-preload.cjs` on webview attach; earlier `dom-ready` hide path.
- Packaged shell loads Electron from the installer asar — this fix ships only via a new installer (not checkout sync alone).
