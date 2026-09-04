#!/bin/bash
# Side-by-side fixture inspect launcher.
#
# scripts/dev.sh is the replace-the-running-instance launcher: it stops
# Electron/Vite, removes the production backend marker, and uses the
# machine HARNESS_STATE_DIR / account context.
#
# This script is the supported way to inspect a source checkout while the
# installed app stays up. It does not kill production, does not share a
# backend or runtime state root, and does not copy or fold long-lived
# credentials. Onboarding is skipped as a fixture (no provider access).
set -euo pipefail

REPO_ROOT="$( cd "$( dirname "${BASH_SOURCE[0]}" )/.." && pwd )"
SLUG="$(printf '%s' "$REPO_ROOT" | shasum -a 256 | cut -c1-12)"
INSPECT_ROOT="${HOME}/.pmharness/inspect/${SLUG}"
export HARNESS_INSPECT=1
export HARNESS_STATE_DIR="${INSPECT_ROOT}/state"
export HARNESS_USER_DATA_DIR="${INSPECT_ROOT}/user-data"
mkdir -p "$HARNESS_STATE_DIR" "$HARNESS_USER_DATA_DIR"

cd "$REPO_ROOT/webapp" || exit 1
echo "Launching Marionette inspect (isolated state, no production credentials)..."
echo "  HARNESS_STATE_DIR=$HARNESS_STATE_DIR"
npm run electron:inspect
