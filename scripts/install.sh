#!/usr/bin/env bash
# Marionette one-line installer (macOS Intel + Apple Silicon, Linux).
#
#   curl -fsSL https://professorpalmer.github.io/marionette/install.sh | bash
#   curl -fsSL https://raw.githubusercontent.com/professorpalmer/marionette/main/scripts/install.sh | bash
#
# Marionette runs from source (the Hermes model): this clones the repo, builds a
# per-machine Python venv with `uv` (which brings its own pinned Python), installs
# node deps, builds the renderer, and drops a `marionette` launcher on your PATH.
# There is no DMG and nothing arch-specific to download -- native modules compile
# locally, so the same script works on Intel Macs, Apple Silicon, and Linux.
#
# Updates after this are in-app: the status-bar "Update" pill pulls + rebuilds.
# Re-running this script is safe: it fast-forwards an existing checkout and
# refreshes the venv + renderer build in place.
set -euo pipefail

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
VERSIONS_FILE="$SCRIPT_DIR/versions.env"
if [ -f "$VERSIONS_FILE" ]; then
  # shellcheck disable=SC1091
  . "$VERSIONS_FILE"
fi

REPO_URL="${MARIONETTE_REPO_URL:-https://github.com/professorpalmer/marionette.git}"
MARIONETTE_HOME="${MARIONETTE_HOME:-$HOME/.marionette}"
DEST="${MARIONETTE_DEST:-$MARIONETTE_HOME/marionette}"
BRANCH="${MARIONETTE_BRANCH:-main}"
BIN_DIR="${MARIONETTE_BIN_DIR:-$HOME/.local/bin}"
NODE_MIN_MAJOR="${MARIONETTE_NODE_MIN_MAJOR:-20}"
PINNED_NODE="${MARIONETTE_NODE_VERSION:-}"

say()  { printf '\n== %s ==\n' "$1"; }
warn() { printf 'WARN: %s\n' "$1" >&2; }
die()  { printf 'ERROR: %s\n' "$1" >&2; exit 1; }
step() { printf '  -> %s\n' "$1"; }

verify_sha256() {
  local file="$1" expected="$2"
  [ -n "$expected" ] || return 0
  local actual=""
  if command -v sha256sum >/dev/null 2>&1; then
    actual="$(sha256sum "$file" | awk '{print $1}')"
  elif command -v shasum >/dev/null 2>&1; then
    actual="$(shasum -a 256 "$file" | awk '{print $1}')"
  else
    warn "no sha256sum/shasum; skipping checksum for $(basename "$file")"
    return 0
  fi
  if [ "$actual" != "$expected" ]; then
    die "checksum mismatch for $(basename "$file") (expected $expected, got $actual)"
  fi
  step "checksum verified ($(basename "$file"))"
}

# --- 0. platform -------------------------------------------------------------
OS="$(uname -s)"
ARCH="$(uname -m)"
case "$OS" in
  Darwin|Linux) : ;;
  *) die "unsupported OS '$OS'. Marionette installs on macOS and Linux via this script; use install.ps1 on Windows." ;;
esac
say "Installing Marionette for $OS/$ARCH"

# --- 1. git (hard requirement) ----------------------------------------------
command -v git >/dev/null 2>&1 || die "'git' is required but not on PATH. Install git and re-run."

# --- 2. uv (brings its own pinned Python) -----------------------------------
if ! command -v uv >/dev/null 2>&1; then
  say "Installing uv (Python toolchain manager)"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  # uv installs to ~/.local/bin (or $XDG_BIN_HOME); make it visible this session.
  export PATH="$HOME/.local/bin:${PATH:-}"
  command -v uv >/dev/null 2>&1 || die "uv installed but not on PATH. Add ~/.local/bin to PATH and re-run."
else
  step "uv already on PATH ($(uv --version 2>/dev/null || echo present))"
fi

# --- 3. Node (enforce, don't just detect) -----------------------------------
ensure_node() {
  if command -v node >/dev/null 2>&1; then
    local major
    major="$(node -v | sed 's/^v//' | cut -d. -f1)"
    if [ "${major:-0}" -ge "$NODE_MIN_MAJOR" ]; then
      step "node $(node -v) meets minimum (>= v${NODE_MIN_MAJOR})"
      return 0
    fi
    warn "node $(node -v) is older than the required v${NODE_MIN_MAJOR}."
  fi
  # Try nvm against the repo's .nvmrc / pinned version if it is available.
  if [ -s "${NVM_DIR:-$HOME/.nvm}/nvm.sh" ]; then
    # shellcheck disable=SC1091
    . "${NVM_DIR:-$HOME/.nvm}/nvm.sh"
    local target="${PINNED_NODE:-}"
    if [ -z "$target" ] && [ -f "$DEST/.nvmrc" ]; then
      target="$(tr -d '[:space:]' < "$DEST/.nvmrc")"
    fi
    if [ -n "$target" ]; then
      say "Installing Node v${target} via nvm"
      nvm install "$target" >/dev/null 2>&1 || true
      nvm use "$target" >/dev/null 2>&1 || true
    elif [ -f "$DEST/.nvmrc" ]; then
      say "Installing the pinned Node via nvm ($(cat "$DEST/.nvmrc"))"
      nvm install >/dev/null 2>&1 || true
      nvm use >/dev/null 2>&1 || true
    fi
  fi
  if command -v node >/dev/null 2>&1; then
    local major
    major="$(node -v | sed 's/^v//' | cut -d. -f1)"
    if [ "${major:-0}" -ge "$NODE_MIN_MAJOR" ]; then
      step "node $(node -v) ready"
      return 0
    fi
  fi
  die "Node >= v${NODE_MIN_MAJOR} is required. Install it (https://nodejs.org or nvm) and re-run."
}

# --- 4. clone or update ------------------------------------------------------
mkdir -p "$MARIONETTE_HOME"
if [ -d "$DEST/.git" ]; then
  say "Existing checkout at $DEST -- fetching $BRANCH"
  git -C "$DEST" fetch --no-tags origin "$BRANCH"
  git -C "$DEST" checkout "$BRANCH"
  git -C "$DEST" merge --ff-only "origin/$BRANCH" || warn "local changes present; skipped fast-forward"
else
  say "Cloning $REPO_URL -> $DEST"
  git clone --branch "$BRANCH" "$REPO_URL" "$DEST"
fi
cd "$DEST"

# Node needs the checkout present first (for .nvmrc), so enforce it here.
ensure_node

# --- 5. Python backend (uv) --------------------------------------------------
say "Provisioning Python via uv (reads .python-version)"
uv python install
if [ -d .venv ]; then
  step "reusing existing .venv"
else
  uv venv .venv
fi
say "Installing Marionette (editable) + Puppetmaster into .venv"
uv pip install --python .venv -e .
# Puppetmaster is the one real runtime dependency; it ships on PyPI as
# puppetmaster-ai. MARIONETTE_PUPPETMASTER_SPEC lets a contributor point at a
# local editable checkout instead (e.g. an absolute path).
uv pip install --python .venv "${MARIONETTE_PUPPETMASTER_SPEC:-puppetmaster-ai==1.20.9}"

# --- 6. renderer -------------------------------------------------------------
say "Installing node deps + building the renderer"
( cd webapp && npm ci && npm run build )

# --- 7. launcher shim --------------------------------------------------------
say "Installing the 'marionette' launcher into $BIN_DIR"
mkdir -p "$BIN_DIR"
cat > "$BIN_DIR/marionette" <<EOF
#!/usr/bin/env bash
# Marionette launcher (installed by scripts/install.sh).
set -euo pipefail
MARIONETTE_DEST="$DEST"
case "\${1:-}" in
  doctor)  exec bash "\$MARIONETTE_DEST/scripts/doctor.sh" ;;
  dev)     exec bash "\$MARIONETTE_DEST/scripts/dev.sh" ;;
  update)  git -C "\$MARIONETTE_DEST" pull --ff-only && ( cd "\$MARIONETTE_DEST/webapp" && npm run build ) ;;
  ""|desktop) exec bash "\$MARIONETTE_DEST/scripts/start.sh" ;;
  *) echo "usage: marionette [desktop|dev|doctor|update]" >&2; exit 2 ;;
esac
EOF
chmod +x "$BIN_DIR/marionette"

# --- 8. final gate: doctor ---------------------------------------------------
say "Verifying the install"
if ! bash "$DEST/scripts/doctor.sh"; then
  warn "doctor reported problems above -- Marionette may not run correctly until they are fixed."
fi

cat <<EOF

Marionette is installed at: $DEST

Launch it:
  marionette            # daily use (built renderer, in-app updates)
  marionette dev        # contributor hot-reload (Vite HMR)
  marionette doctor     # re-check the environment
  marionette update     # git pull + rebuild

If 'marionette' is not found, add this to your shell profile:
  export PATH="$BIN_DIR:\$PATH"

Set your API keys in the app's Settings pane, or in ~/.pmharness/keys.json.
EOF
