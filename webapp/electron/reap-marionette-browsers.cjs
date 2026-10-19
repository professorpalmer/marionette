"use strict";

const os = require("node:os");
const path = require("node:path");

const BROWSER_HINTS = ["chrome", "chromium", "msedge", "brave", "google chrome"];
const TEMP_PROFILE_PREFIX = "pm-cdp-";

// Profile roots under this instance's HOME. Exact paths, not substrings:
// a sibling install (other HOME) has its own `.pmharness/browser-profile`.
function ownProfileRoots(home = os.homedir(), platform = process.platform) {
  const { join } = platform === "win32" ? path.win32 : path.posix;
  return [
    join(home, ".pmharness", "browser-profile"),
    join(home, ".pmharness", "browser-profile-real"),
    join(home, ".puppetmaster", "browser-profile"),
    join(home, ".puppetmaster", "browser-profiles"),
  ];
}

function normalizeDir(dir, platform) {
  let out = String(dir || "").trim().replace(/^["']|["']$/g, "");
  if (platform === "win32") out = out.replace(/\\/g, "/");
  out = out.replace(/\/+$/, "");
  // Windows and default macOS volumes ignore case.
  return platform === "win32" || platform === "darwin" ? out.toLowerCase() : out;
}

// Value of --user-data-dir. The path can hold spaces, so it ends at the next
// " --flag", not at the next space.
function userDataDir(cmdline) {
  const raw = typeof cmdline === "string" ? cmdline : "";
  const start = raw.indexOf("--user-data-dir=");
  if (start < 0) return "";
  const rest = raw.slice(start + "--user-data-dir=".length);
  const end = rest.search(/\s--[a-z]/i);
  return (end < 0 ? rest : rest.slice(0, end)).trim();
}

function isBrowser(cmdline) {
  const low = String(cmdline || "").toLowerCase();
  return BROWSER_HINTS.some((name) => low.includes(name));
}

// "home": a browser on one of this HOME's profile roots.
// "temp": a throwaway pm-cdp-* profile, which every install shares by name;
//         the caller reaps it only when the owner chain allows.
function profileOwnership(cmdline, { home = os.homedir(), platform = process.platform } = {}) {
  if (!isBrowser(cmdline)) return null;
  const dir = normalizeDir(userDataDir(cmdline), platform);
  if (!dir) return null;
  for (const root of ownProfileRoots(home, platform)) {
    const r = normalizeDir(root, platform);
    if (dir === r || dir.startsWith(`${r}/`)) return "home";
  }
  if (path.posix.basename(dir).startsWith(TEMP_PROFILE_PREFIX)) return "temp";
  return null;
}

function cmdlineIsMarionetteBrowser(cmdline, opts) {
  return profileOwnership(cmdline, opts) === "home";
}

// A temp-profile browser is ours when this process is an ancestor, or
// orphaned when its parent is gone. A live foreign parent means another
// Marionette instance still drives it.
function tempProfileReapable(pid, parents, selfPid) {
  const parent = parents.get(pid);
  if (!parent || parent <= 1 || !parents.has(parent)) return true;
  const seen = new Set();
  for (let cur = parent; cur && cur > 1 && !seen.has(cur); cur = parents.get(cur)) {
    if (cur === selfPid) return true;
    seen.add(cur);
  }
  return false;
}

function parsePosixPs(text) {
  const rows = [];
  for (const line of String(text || "").split(/\r?\n/)) {
    const match = line.match(/^\s*(\d+)\s+(\d+)\s+(.*)$/);
    if (!match) continue;
    const pid = Number(match[1]);
    if (!Number.isInteger(pid) || pid <= 1) continue;
    rows.push({ pid, ppid: Number(match[2]), cmdline: match[3] });
  }
  return rows;
}

function parseWmicList(text) {
  const rows = [];
  let row = { pid: 0, ppid: 0, cmdline: "" };
  const flush = () => {
    if (row.pid > 1) rows.push(row);
    row = { pid: 0, ppid: 0, cmdline: "" };
  };
  for (const line of String(text || "").split(/\r?\n/)) {
    const raw = line.trim();
    if (!raw) {
      flush();
      continue;
    }
    const value = raw.slice(raw.indexOf("=") + 1);
    if (/^commandline=/i.test(raw)) row.cmdline = value;
    else if (/^parentprocessid=/i.test(raw)) row.ppid = Number(value) || 0;
    else if (/^processid=/i.test(raw)) row.pid = Number(value) || 0;
  }
  flush();
  return rows;
}

function reapTargets(rows, { home, platform, selfPid }) {
  const parents = new Map(rows.map((row) => [row.pid, row.ppid]));
  return rows
    .filter((row) => {
      const kind = profileOwnership(row.cmdline, { home, platform });
      if (kind === "home") return true;
      return kind === "temp" && tempProfileReapable(row.pid, parents, selfPid);
    })
    .map((row) => row.pid);
}

function reapMarionetteBrowsers({
  spawnSync,
  platform = process.platform,
  home = os.homedir(),
  selfPid = process.pid,
} = {}) {
  if (typeof spawnSync !== "function") return { signaled: [] };
  const signaled = [];
  if (platform === "win32") {
    const listed = spawnSync(
      "wmic",
      ["process", "get", "ProcessId,ParentProcessId,CommandLine", "/FORMAT:LIST"],
      { encoding: "utf8", timeout: 8000, windowsHide: true }
    );
    const rows = parseWmicList(listed && listed.stdout ? listed.stdout : "");
    for (const pid of reapTargets(rows, { home, platform, selfPid })) {
      spawnSync("taskkill", ["/PID", String(pid), "/T", "/F"], {
        timeout: 5000,
        windowsHide: true,
      });
      signaled.push(pid);
    }
    return { signaled };
  }
  const listed = spawnSync("ps", ["-axww", "-o", "pid=,ppid=,args="], {
    encoding: "utf8",
    timeout: 8000,
  });
  const rows = parsePosixPs(listed && listed.stdout ? listed.stdout : "");
  for (const pid of reapTargets(rows, { home, platform, selfPid })) {
    try {
      process.kill(pid, "SIGTERM");
      signaled.push(pid);
    } catch {
      /* already gone */
    }
  }
  return { signaled };
}

module.exports = {
  cmdlineIsMarionetteBrowser,
  parsePosixPs,
  parseWmicList,
  profileOwnership,
  reapMarionetteBrowsers,
  userDataDir,
};
