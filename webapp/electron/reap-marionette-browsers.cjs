"use strict";

const PROFILE_MARKERS = [
  "/.pmharness/browser-profile",
  "\\.pmharness\\browser-profile",
  "/.puppetmaster/browser-profile",
  "\\.puppetmaster\\browser-profile",
  "/pm-cdp-",
  "\\pm-cdp-",
];
const BROWSER_HINTS = ["chrome", "chromium", "msedge", "brave", "google chrome"];

function cmdlineIsMarionetteBrowser(cmdline) {
  const raw = typeof cmdline === "string" ? cmdline : "";
  const low = raw.toLowerCase();
  if (!BROWSER_HINTS.some((name) => low.includes(name))) return false;
  if (!low.includes("--user-data-dir=")) return false;
  return PROFILE_MARKERS.some((marker) => low.includes(marker.toLowerCase()));
}

function parsePosixPs(text) {
  const rows = [];
  for (const line of String(text || "").split(/\r?\n/)) {
    const match = line.match(/^\s*(\d+)\s+(.*)$/);
    if (!match) continue;
    const pid = Number(match[1]);
    if (!Number.isInteger(pid) || pid <= 1) continue;
    rows.push({ pid, cmdline: match[2] });
  }
  return rows;
}

function reapMarionetteBrowsers({ spawnSync, platform = process.platform } = {}) {
  if (typeof spawnSync !== "function") return { signaled: [] };
  const signaled = [];
  if (platform === "win32") {
    const listed = spawnSync(
      "wmic",
      ["process", "get", "ProcessId,CommandLine", "/FORMAT:LIST"],
      { encoding: "utf8", timeout: 8000, windowsHide: true }
    );
    const text = listed && listed.stdout ? String(listed.stdout) : "";
    let pid = 0;
    let cmd = "";
    const flush = () => {
      if (pid > 1 && cmdlineIsMarionetteBrowser(cmd)) {
        spawnSync("taskkill", ["/PID", String(pid), "/T", "/F"], {
          timeout: 5000,
          windowsHide: true,
        });
        signaled.push(pid);
      }
      pid = 0;
      cmd = "";
    };
    for (const line of text.split(/\r?\n/)) {
      const raw = line.trim();
      if (!raw) {
        flush();
        continue;
      }
      if (/^commandline=/i.test(raw)) cmd = raw.slice(raw.indexOf("=") + 1);
      else if (/^processid=/i.test(raw)) pid = Number(raw.split("=")[1]) || 0;
    }
    flush();
    return { signaled };
  }
  const listed = spawnSync("ps", ["-axww", "-o", "pid=,args="], {
    encoding: "utf8",
    timeout: 8000,
  });
  const rows = parsePosixPs(listed && listed.stdout ? listed.stdout : "");
  for (const row of rows) {
    if (!cmdlineIsMarionetteBrowser(row.cmdline)) continue;
    try {
      process.kill(row.pid, "SIGTERM");
      signaled.push(row.pid);
    } catch {
      /* already gone */
    }
  }
  return { signaled };
}

module.exports = {
  cmdlineIsMarionetteBrowser,
  parsePosixPs,
  reapMarionetteBrowsers,
};
