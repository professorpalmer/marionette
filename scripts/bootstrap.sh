#!/usr/bin/env bash
# Compatibility shim: bootstrap.sh has been superseded by install.sh (the
# uv-based source-run installer). It delegates so old links keep working.
#
#   curl -fsSL https://raw.githubusercontent.com/professorpalmer/marionette/main/scripts/install.sh | bash
set -euo pipefail
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
exec bash "$DIR/install.sh" "$@"
