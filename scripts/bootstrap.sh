#!/bin/bash
# One-command Marionette bootstrap for a new machine (macOS, Apple Silicon).
#
# Clones the repo, builds a Python venv + node deps, compiles the renderer, and
# tells you how to launch. After this, updates are in-app: Marionette tracks the
# git checkout and "Update & Relaunch" pulls + rebuilds in place (no DMG).
#
# Run from anywhere:
#   curl -fsSL https://raw.githubusercontent.com/professorpalmer/pm-harness/main/scripts/bootstrap.sh | bash
# Or, in an existing clone:
#   bash scripts/bootstrap.sh
set -euo pipefail

REPO_URL="${MARIONETTE_REPO_URL:-https://github.com/professorpalmer/pm-harness.git}"
DEST="${MARIONETTE_HOME:-$HOME/pm-harness}"
BRANCH="${MARIONETTE_BRANCH:-main}"

say() { printf '\n== %s ==\n' "$1"; }

# --- preflight: required toolchain ------------------------------------------
missing=0
for tool in git python3 node npm; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    echo "ERROR: '$tool' is required but not on PATH." >&2
    missing=1
  fi
done
[ "$missing" -eq 0 ] || { echo "Install the missing tools and re-run." >&2; exit 1; }

# --- clone or update ---------------------------------------------------------
if [ -d "$DEST/.git" ]; then
  say "Existing checkout at $DEST -- fetching $BRANCH"
  git -C "$DEST" fetch --no-tags origin "$BRANCH"
  git -C "$DEST" checkout "$BRANCH"
  git -C "$DEST" merge --ff-only "origin/$BRANCH" || echo "(local changes present; skipping fast-forward)"
else
  say "Cloning $REPO_URL -> $DEST"
  git clone --branch "$BRANCH" "$REPO_URL" "$DEST"
fi
cd "$DEST"

# --- Python backend ----------------------------------------------------------
say "Setting up Python venv (.venv)"
[ -d .venv ] || python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip >/dev/null
.venv/bin/python -m pip install -e ".[dev]" puppetmaster-ai

# --- renderer ----------------------------------------------------------------
say "Installing node deps + building the renderer"
( cd webapp && npm ci && npm run build )

say "Done"
cat <<EOF

Marionette is ready at: $DEST

Launch it (daily use, with in-app updates):
  bash $DEST/scripts/start.sh

Contributor mode (Vite hot-reload for editing):
  bash $DEST/scripts/dev.sh

Optional shell alias (add to ~/.zshrc):
  alias marionette='bash $DEST/scripts/start.sh'

Set your API keys in the app's Settings pane, or in ~/.pmharness/keys.json.
Updates: when you or a friend merge to '$BRANCH', the status bar shows an
"update (N)" pill -- click it to pull + rebuild + relaunch. No DMG needed.
EOF
