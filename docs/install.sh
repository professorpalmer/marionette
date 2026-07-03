#!/usr/bin/env bash
# Thin shim: fetch and exec the canonical Marionette installer from main.
# Published at https://professorpalmer.github.io/marionette/install.sh
set -euo pipefail
exec bash <(curl -fsSL "https://raw.githubusercontent.com/professorpalmer/marionette/main/scripts/install.sh")
