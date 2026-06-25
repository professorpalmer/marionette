#!/bin/bash
set -e

# Resolve the directory where this script resides
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
REPO_ROOT="$( cd "$SCRIPT_DIR/.." && pwd )"

cd "$REPO_ROOT"

# Ensure we use the venv pyinstaller
PYINSTALLER=".venv/bin/pyinstaller"

if [ ! -f "$PYINSTALLER" ]; then
    echo "Error: pyinstaller not found in .venv. Please install it first."
    exit 1
fi

echo "Building self-contained backend binary with PyInstaller..."
"$PYINSTALLER" --clean --distpath webapp/backend-dist --workpath build/pyinstaller-work build/pmharness-backend.spec

echo "Backend binary build complete!"
