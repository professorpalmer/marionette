#!/bin/bash
# Launch Marionette in dev mode (source build, hot-reload). Just run: marionette
# or: bash scripts/dev.sh from any checkout.
set -euo pipefail

REPO_ROOT="$( cd "$( dirname "${BASH_SOURCE[0]}" )/.." && pwd )"
cd "$REPO_ROOT/webapp" || exit 1

# Clean up any stale dev stack first so we never collide on ports.
pkill -f "electron:dev" 2>/dev/null || true
pkill -f "PMHARNESS_DEV_SERVER" 2>/dev/null || true
pkill -f "vite --host 127.0.0.1 --port 5273" 2>/dev/null || true
pkill -f "Electron" 2>/dev/null || true
rm -f "${HOME}/.pmharness/backend.json" 2>/dev/null || true
sleep 1

echo "Launching Marionette (dev)..."
npm run electron:dev
