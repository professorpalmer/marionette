"use strict";

// Build the environment for the self-updater's child processes (git, npm, uv).
//
// A packaged app launched from Finder/Dock inherits a MINIMAL launchd env: it is
// missing the user's real PATH (and SSH_AUTH_SOCK, etc.). So npm/uv -- installed
// under Homebrew, a Node version manager, or ~/.local/bin -- are not on PATH and
// spawn with ENOENT ("Update failed: spawn npm ENOENT"), while git still resolves
// from /usr/bin (which is why the source pulls but the rebuild fails).
//
// main.cjs recovers the user's login-shell environment (loginShellEnv); here we
// merge it so the fuller PATH and the shell's other vars are present for the
// update's child processes -- resolving tools exactly as the user's terminal
// would. PATH becomes shellPATH : basePATH (order-preserving, de-duplicated) so
// version-manager/Homebrew dirs win but nothing from the base PATH is dropped.

const path = require("node:path");

function buildUpdaterEnv({ processEnv = {}, shellEnv = {} } = {}) {
  const merged = { ...shellEnv, ...processEnv };
  const parts = [];
  const pushEntries = (value) => {
    if (!value) return;
    for (const seg of String(value).split(path.delimiter)) {
      if (seg && !parts.includes(seg)) parts.push(seg);
    }
  };
  // Login-shell PATH first (npm/uv/version managers live here), then the base
  // launchd PATH as a fallback so /usr/bin etc. are never lost.
  pushEntries(shellEnv.PATH);
  pushEntries(processEnv.PATH);
  if (parts.length) merged.PATH = parts.join(path.delimiter);
  return merged;
}

module.exports = { buildUpdaterEnv };
