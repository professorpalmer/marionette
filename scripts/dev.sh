#!/bin/bash
# Launch Marionette in dev mode (source build, hot-reload). Just run: marionette
# or: bash ~/pm-harness/scripts/dev.sh
cd ~/pm-harness/webapp || exit 1

# Clean up any stale dev stack first so we never collide on ports.
pkill -f "electron:dev" 2>/dev/null
pkill -f "PMHARNESS_DEV_SERVER" 2>/dev/null
pkill -f "vite --host 127.0.0.1 --port 5273" 2>/dev/null
pkill -f "Electron" 2>/dev/null
rm -f ~/.pmharness/backend.json 2>/dev/null
sleep 1

echo "Launching Marionette (dev)..."
npm run electron:dev
