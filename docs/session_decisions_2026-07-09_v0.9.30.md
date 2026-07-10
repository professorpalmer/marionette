# Session decisions — 2026-07-09 (v0.9.30)

## Soften History-panel ECONNREFUSED on backend respawn

- Electron `backendRequest` retries transient loopback refusals (ECONNREFUSED/ECONNRESET) and waits on in-flight `startBackend` when present.
- Preload exposes `onBackendRespawned`; CheckpointsPane re-fetches and shows a softer "starting up" message instead of the raw IPC error.
