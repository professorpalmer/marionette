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
//
// On Windows, GUI launches inherit a stripped PATH that omits npm globals,
// nvm-windows, fnm, uv, and other profile-local tool dirs. windowsShellEnv()
// reads User/Machine PATH from the registry and prepends known profile locations.

const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { execFileSync } = require("node:child_process");

function mergePathStrings(...pathValues) {
  const parts = [];
  const seen = new Set();
  for (const value of pathValues) {
    if (!value) continue;
    for (const seg of String(value).split(path.delimiter)) {
      if (!seg || seen.has(seg)) continue;
      seen.add(seg);
      parts.push(seg);
    }
  }
  return parts.join(path.delimiter);
}

function buildUpdaterEnv({ processEnv = {}, shellEnv = {} } = {}) {
  const merged = { ...shellEnv, ...processEnv };
  const combined = mergePathStrings(shellEnv.PATH, processEnv.PATH);
  if (combined) merged.PATH = combined;
  return merged;
}

function parseRegQueryPath(output) {
  for (const line of String(output).split(/\r?\n/)) {
    const m = line.trim().match(/^PATH\s+REG_(?:EXPAND_)?(?:SZ|MULTI_SZ)\s+(.*)$/i);
    if (m) return m[1].trim();
  }
  return "";
}

function expandWinEnv(value, env) {
  let s = String(value || "");
  for (let i = 0; i < 12; i++) {
    const next = s.replace(/%([^%]+)%/gi, (_, key) => {
      const v = env[key] ?? env[key.toUpperCase()];
      return v != null ? String(v) : `%${key}%`;
    });
    if (next === s) break;
    s = next;
  }
  return s;
}

function readRegistryPath(hive) {
  try {
    const out = execFileSync("reg", ["query", hive, "/v", "PATH"], {
      encoding: "utf8",
      windowsHide: true,
      stdio: ["ignore", "pipe", "ignore"],
      timeout: 5000,
    });
    return parseRegQueryPath(out);
  } catch {
    return "";
  }
}

function newestNvmWindowsBin(nvmHome) {
  try {
    const versions = fs.readdirSync(nvmHome)
      .filter((v) => /^v?\d/.test(v))
      .sort()
      .reverse();
    for (const v of versions) {
      const dir = path.join(nvmHome, v);
      if (fs.existsSync(path.join(dir, "node.exe"))) return dir;
    }
  } catch { /* no nvm */ }
  return "";
}

function windowsProfilePathCandidates(env = process.env) {
  const home = env.USERPROFILE || os.homedir();
  const localAppData = env.LOCALAPPDATA || path.join(home, "AppData", "Local");
  const appData = env.APPDATA || path.join(home, "AppData", "Roaming");
  const candidates = [];

  const push = (dir) => { if (dir) candidates.push(dir); };

  // Marionette portable bootstrap tools (same dirs reinjectPortableTools uses).
  push(path.join(localAppData, "marionette", "tools", "node"));
  push(path.join(localAppData, "marionette", "tools", "git", "cmd"));

  // uv / cargo / npm globals (bootstrap.cjs ensureUv targets these).
  push(path.join(home, ".local", "bin"));
  push(path.join(home, ".cargo", "bin"));
  push(path.join(appData, "npm"));

  // Node version managers common on Windows.
  push(env.NVM_SYMLINK);
  push(newestNvmWindowsBin(env.NVM_HOME || path.join(appData, "nvm")));
  push(path.join(home, ".volta", "bin"));
  push(path.join(localAppData, "fnm"));
  push(path.join(home, ".fnm", "aliases", "default"));

  // Common package-manager shims. Use path.win32 so unit tests on Linux CI
  // still assert the Windows-shaped candidate strings this helper exists for.
  const win = path.win32;
  push(path.join(home, "scoop", "shims"));
  push(win.join("C:", "ProgramData", "chocolatey", "bin"));

  // Stock Node/Git MSI installs (existingProfilePaths filters to dirs on disk).
  push(win.join("C:", "Program Files", "nodejs"));
  push(win.join("C:", "Program Files (x86)", "nodejs"));
  push(win.join("C:", "Program Files", "Git", "cmd"));
  push(win.join("C:", "Program Files (x86)", "Git", "cmd"));

  return candidates;
}

function existingProfilePaths(candidates, fsImpl = fs) {
  return candidates.filter((dir) => {
    try { return fsImpl.existsSync(dir); } catch { return false; }
  });
}

// Windows equivalent of macOS loginShellEnv: recover the user's real PATH for
// GUI launches without spawning a visible console. Registry User+Machine PATH
// plus known profile tool locations are merged ahead of the inherited PATH.
function windowsShellEnv(env = process.env) {
  if (process.platform !== "win32") return {};

  const baseEnv = { ...env };
  const userReg = expandWinEnv(readRegistryPath("HKCU\\Environment"), baseEnv);
  const machineReg = expandWinEnv(
    readRegistryPath("HKLM\\SYSTEM\\CurrentControlSet\\Control\\Session Manager\\Environment"),
    baseEnv,
  );

  const profileDirs = existingProfilePaths(windowsProfilePathCandidates(baseEnv));
  const profilePath = profileDirs.join(path.delimiter);

  const PATH = mergePathStrings(profilePath, userReg, machineReg, env.PATH);
  return PATH ? { PATH } : {};
}

module.exports = {
  buildUpdaterEnv,
  mergePathStrings,
  parseRegQueryPath,
  expandWinEnv,
  windowsProfilePathCandidates,
  windowsShellEnv,
};
