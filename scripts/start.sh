#!/bin/bash
# Launch Marionette for daily use from this checkout (production renderer, NOT
# the Vite dev server), so the in-app self-updater can pull + rebuild + relaunch.
#
# This is the mode friends run: it loads webapp/dist (built ahead of time) and
# spawns the Python backend from the repo's .venv (created by install.sh via uv).
# The "Update & Relaunch" pill rebuilds dist in place. For active editing with
# hot-reload, use dev.sh instead (or `marionette dev`).
set -euo pipefail

REPO_ROOT="$( cd "$( dirname "${BASH_SOURCE[0]}" )/.." && pwd )"

if [ ! -x "$REPO_ROOT/.venv/bin/python" ]; then
  echo "No .venv found at $REPO_ROOT/.venv. Run scripts/install.sh (or 'marionette' via the installer) first." >&2
  exit 1
fi

cd "$REPO_ROOT/webapp"

# Build the renderer if it hasn't been built yet (fresh clone / after a pull).
if [ ! -f dist/index.html ]; then
  echo "Building renderer (first run)..."
  npm run build
fi

# Clear a stale backend marker so we spawn a fresh backend on the current code.
rm -f "$HOME/.pmharness/backend.json" 2>/dev/null || true

echo "Launching Marionette..."
exec npm run electron
