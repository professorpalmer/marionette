# Marionette webapp

The shipping Marionette desktop UI: Electron main/preload (`electron/`) plus the
React renderer (`src/`). The stdlib Python backend in `harness/server.py` streams
SSE events to this renderer.

**Run (dev):** from repo root, `marionette dev` or `cd webapp && npm run electron:dev`.

**Build:** `npm run build` (renderer only) or `npm run dist` (packaged shell).

See the repo [README](../README.md) and [ARCHITECTURE.md](../ARCHITECTURE.md) for
the full system map. Do not edit `harness/web/` for product UI work -- that tree
is the legacy browser fallback only.
