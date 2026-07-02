// Binary auto-updater for the installed (.app) build -- the "install once,
// updates just land" model, like Hermes Desktop. New releases published to
// GitHub Releases are downloaded in the background and applied on relaunch, so
// there is no script to run and no DMG to re-download per change.
//
// It implements the SAME IPC contract as the git self-updater (update-bridge.cjs):
//   updates:check    -> { current, available, behind, branch, latest, url }
//   updates:apply    -> streams "updates:progress" { stage, message, percent }, then relaunches
//   updates:openRepo -> opens the releases page
// so the StatusBar pill behaves identically whether the app runs from a git
// checkout (git pull + rebuild) or as a packaged binary (this path).
//
// main.cjs chooses which updater to register based on app.isPackaged: a checkout
// rebuilds itself; a shipped .app swaps its whole signed bundle. macOS refuses to
// apply an unsigned update, so the release must be Developer ID signed + notarized.

const REPO_HTML_URL = "https://github.com/professorpalmer/pm-harness";
const RELEASES_URL = `${REPO_HTML_URL}/releases`;
const CHECK_INTERVAL_MS = 30 * 60 * 1000; // re-check every 30m while running
// Also re-check when the user refocuses the app, but no more than once per this
// window -- so tabbing in and out doesn't hammer GitHub, while a release cut
// while you were away is noticed the moment you come back.
const FOCUS_RECHECK_MIN_MS = 5 * 60 * 1000;

// Compare dotted numeric versions ("0.6.7"). Returns >0 if a is newer than b.
function compareVersions(a, b) {
  const pa = String(a).split(".").map((n) => parseInt(n, 10) || 0);
  const pb = String(b).split(".").map((n) => parseInt(n, 10) || 0);
  for (let i = 0; i < Math.max(pa.length, pb.length); i += 1) {
    const diff = (pa[i] || 0) - (pb[i] || 0);
    if (diff !== 0) return diff;
  }
  return 0;
}

function registerAutoUpdater(ipcMain, app, shell, opts = {}) {
  let autoUpdater;
  try {
    ({ autoUpdater } = require("electron-updater"));
  } catch {
    // Dependency unavailable: expose a no-op surface so the renderer's update
    // check simply reports "up to date" instead of throwing.
    ipcMain.handle("updates:check", async () => ({ current: app.getVersion(), available: false }));
    ipcMain.handle("updates:apply", async () => ({ ok: false, error: "auto-updater unavailable" }));
    ipcMain.handle("updates:openRepo", async () => {
      try { await shell.openExternal(RELEASES_URL); return true; } catch { return false; }
    });
    return;
  }

  const cleanup = opts.cleanup || (() => {});

  autoUpdater.autoDownload = true; // pull new releases in the background
  autoUpdater.autoInstallOnAppQuit = true; // apply on the next quit with no prompt
  autoUpdater.allowPrerelease = false;
  autoUpdater.logger = null;
  // Force a full-zip download instead of a blockmap delta. This bundle is large
  // and packed with frameworks + symlinks (Electron, Squirrel, bundled Python,
  // node, codegraph wasm); electron-updater's macOS differential downloader
  // reconstructs the new .app by ditto-cloning the installed one and patching
  // changed blocks, and it chokes on that framework/symlink layout -- ditto fails
  // with "No such file or directory" and it retries relentlessly (observed 100+
  // update.XXXX staging dirs and 170+ unhandledRejections in a single update
  // window) before finally falling back to a full download. The install itself
  // always succeeded (ShipIt status 0), so the delta path was pure thrash + log
  // spam. A full download is slightly larger but reliable and quiet.
  autoUpdater.disableDifferentialDownload = true;

  const state = { available: false, downloaded: false, latest: app.getVersion(), error: "" };
  let installWhenReady = false;

  const { BrowserWindow } = require("electron");
  const broadcast = (payload) => {
    for (const win of BrowserWindow.getAllWindows()) {
      try {
        if (win.webContents && !win.webContents.isDestroyed()) win.webContents.send("updates:progress", payload);
      } catch { /* window went away mid-broadcast */ }
    }
  };

  const install = () => {
    // The bytes are already on disk by this point; the only work left is the
    // bundle swap + relaunch. Say "Installing", not a download percent, so the
    // pill never looks stuck at 0% right before the app restarts.
    broadcast({ stage: "install", message: "Installing update -- restarting", percent: null });
    try { cleanup(); } catch { /* ignore */ }
    // A tick lets the renderer paint the final state before the app quits.
    setTimeout(() => {
      try { autoUpdater.quitAndInstall(false, true); } catch { /* ignore */ }
    }, 400);
  };

  autoUpdater.on("update-available", (info) => {
    state.available = true;
    state.latest = (info && info.version) || state.latest;
    broadcast({ stage: "available", message: `Update ${state.latest} available`, percent: 0 });
  });
  autoUpdater.on("download-progress", (p) => {
    const pct = Math.round((p && p.percent) || 0);
    // Only surface a percent once there's real progress. A differential
    // (blockmap) download can jump straight from 0 to done, so painting "0%"
    // just reads as "stuck".
    if (pct > 0) {
      broadcast({ stage: "download", message: `Downloading update ${pct}%`, percent: pct });
    } else {
      broadcast({ stage: "prepare", message: "Preparing update", percent: null });
    }
  });
  autoUpdater.on("update-downloaded", (info) => {
    state.downloaded = true;
    state.latest = (info && info.version) || state.latest;
    broadcast({ stage: "downloaded", message: `Update ${state.latest} ready`, percent: 100 });
    if (installWhenReady) install();
  });
  autoUpdater.on("error", (err) => {
    state.error = String((err && err.message) || err);
  });

  let lastCheckAt = 0;
  const check = async () => {
    lastCheckAt = Date.now();
    try {
      const result = await autoUpdater.checkForUpdates();
      const info = result && result.updateInfo;
      const latest = (info && info.version) || app.getVersion();
      state.latest = latest;
      state.available = compareVersions(latest, app.getVersion()) > 0;
      return state.available;
    } catch (e) {
      // A failed check must stay silent -- never nag on a flaky network.
      state.error = String((e && e.message) || e);
      return false;
    }
  };

  ipcMain.handle("updates:check", async () => {
    await check();
    return {
      current: app.getVersion(),
      available: state.available,
      downloaded: state.downloaded,
      behind: state.available ? 1 : 0,
      branch: `v${state.latest}`,
      latest: state.latest,
      url: RELEASES_URL,
    };
  });

  ipcMain.handle("updates:apply", async () => {
    if (!state.available && !state.downloaded) {
      const has = await check();
      if (!has) return { ok: false, error: "no update available" };
    }
    if (state.downloaded) {
      install();
      return { ok: true };
    }
    // Download is (or will be) in flight; install as soon as it lands. Show a
    // neutral "Preparing" (no 0%) until real download progress arrives.
    installWhenReady = true;
    broadcast({ stage: "prepare", message: "Preparing update", percent: null });
    try { autoUpdater.downloadUpdate(); } catch { /* autoDownload may already be running */ }
    return { ok: true };
  });

  ipcMain.handle("updates:openRepo", async () => {
    try { await shell.openExternal(RELEASES_URL); return true; } catch { return false; }
  });

  // Kick an initial background check shortly after launch, then periodically.
  setTimeout(() => { void check(); }, 8000);
  setInterval(() => { void check(); }, CHECK_INTERVAL_MS);

  // Refocusing the app re-checks (rate-limited), so a release cut while you were
  // away shows up when you come back instead of only on the next 30m tick.
  app.on("browser-window-focus", () => {
    if (Date.now() - lastCheckAt >= FOCUS_RECHECK_MIN_MS) void check();
  });
}

module.exports = { registerAutoUpdater, compareVersions };
