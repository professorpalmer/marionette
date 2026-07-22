#!/usr/bin/env bash
# marionette doctor -- verify a source-run install is healthy.
#
# Source-run trades a frozen binary for local compilation, so the failure modes
# are environmental: wrong Python/Node, a missing C/C++ compiler for CodeGraph's
# better-sqlite3, an unbuilt renderer, or a venv that can't import the kernel.
# This checks each and prints the exact fix rather than letting a later run
# explode with a raw node-gyp / import trace. Exit non-zero if anything critical
# fails so install.sh can gate on it.
set -uo pipefail

REPO_ROOT="$( cd "$( dirname "${BASH_SOURCE[0]}" )/.." && pwd )"
cd "$REPO_ROOT"

FAIL=0
ok()   { printf '  ok    %s\n' "$1"; }
bad()  { printf '  FAIL  %s\n' "$1" >&2; FAIL=1; }
note() { printf '  note  %s\n' "$1"; }

printf '== marionette doctor (%s) ==\n' "$REPO_ROOT"

# --- Python venv + kernel importability -------------------------------------
PY=".venv/bin/python"
if [ -x "$PY" ]; then
  ok "python venv present ($($PY --version 2>&1))"
  if $PY -c "import harness" 2>/dev/null; then ok "imports 'harness'"; else bad "cannot import 'harness' -- run: uv pip install --python .venv -e ."; fi
  if $PY -c "import puppetmaster" 2>/dev/null; then ok "imports 'puppetmaster'"; else bad "cannot import 'puppetmaster' -- run: uv pip install --python .venv puppetmaster-ai==1.20.8"; fi
else
  bad "no .venv/bin/python -- run: uv venv .venv && uv pip install --python .venv -e ."
fi

# --- Node --------------------------------------------------------------------
if command -v node >/dev/null 2>&1; then
  NODE_MAJOR="$(node -v | sed 's/^v//' | cut -d. -f1)"
  if [ "${NODE_MAJOR:-0}" -ge 20 ]; then ok "node $(node -v)"; else bad "node $(node -v) too old (need >= v20)"; fi
  if [ -f .nvmrc ]; then note "pinned Node is $(cat .nvmrc) (.nvmrc)"; fi
else
  bad "node not on PATH (need >= v20)"
fi

# --- C/C++ toolchain (better-sqlite3 native build) --------------------------
if command -v cc >/dev/null 2>&1 || command -v gcc >/dev/null 2>&1 || command -v clang >/dev/null 2>&1; then
  ok "C/C++ compiler present (CodeGraph native build can compile)"
else
  if [ "$(uname -s)" = "Darwin" ]; then
    bad "no compiler -- run: xcode-select --install"
  else
    bad "no compiler -- install build-essential (Debian/Ubuntu) or the Development Tools group"
  fi
fi

# --- renderer build ----------------------------------------------------------
if [ -f webapp/dist/index.html ]; then ok "renderer built (webapp/dist)"; else bad "renderer not built -- run: (cd webapp && npm run build)"; fi

# --- optional external tools -------------------------------------------------
command -v rg     >/dev/null 2>&1 && ok "ripgrep (rg) present"     || note "ripgrep (rg) not found -- some search is slower without it"
command -v ffmpeg >/dev/null 2>&1 && ok "ffmpeg present"           || note "ffmpeg not found -- optional (media features)"

# --- CodeGraph binding -------------------------------------------------------
if [ -x "$PY" ] && $PY -c "import puppetmaster" 2>/dev/null; then
  if $PY -m puppetmaster codegraph status >/dev/null 2>&1; then
    ok "CodeGraph binding loads (python -m puppetmaster codegraph)"
  else
    note "CodeGraph status returned non-zero -- may just be an unindexed dir; check: python -m puppetmaster codegraph status"
  fi
fi

echo
if [ "$FAIL" -ne 0 ]; then
  echo "doctor: problems found (see FAIL lines above)."
  exit 1
fi
echo "doctor: all critical checks passed."
