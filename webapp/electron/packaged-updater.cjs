"use strict";

// Packaged-shell updater: electron-updater against GitHub Releases.
//
// Packaged Marionette is a thin Electron shell; backend/renderer live in the
// ~/.marionette/marionette checkout and update via git. The shell itself is
// frozen in app.asar until a signed installer replaces it. This module owns
// that shell path -- check / download / quitAndInstall -- and emits the same
// updates:progress / updates:available channels the git updater uses so the
// existing banner/pill UIs work without a second control plane.
//
// Dev and source-tree runs never activate this path (app.isPackaged is false).
// MARIONETTE_PACKAGED_UPDATER=0 disables it for support / hermetic tests.

const path = require("node:path");
const fs = require("node:fs");

const GITHUB_OWNER = "professorpalmer";
const GITHUB_REPO = "marionette";

/** Semver-ish compare: a > b => 1, a < b => -1, equal => 0. */
function compareVersions(a, b) {
  const norm = (v) => String(v || "").replace(/^v/i, "").trim();
  const pa = norm(a).split(/[.+-]/).map((p) => (/^\d+$/.test(p) ? Number(p) : p));
  const pb = norm(b).split(/[.+-]/).map((p) => (/^\d+$/.test(p) ? Number(p) : p));
  const n = Math.max(pa.length, pb.length);
  for (let i = 0; i < n; i++) {
    const x = pa[i] == null ? 0 : pa[i];
    const y = pb[i] == null ? 0 : pb[i];
    if (typeof x === "number" && typeof y === "number") {
      if (x !== y) return x > y ? 1 : -1;
      continue;
    }
    const sx = String(x);
    const sy = String(y);
    if (sx !== sy) return sx > sy ? 1 : -1;
  }
  return 0;
}

/** True when the frozen shell is older than the checkout package version. */
function shellBehindCheckout({ shellVersion, checkoutVersion }) {
  if (!shellVersion || !checkoutVersion) return false;
  return compareVersions(shellVersion, checkoutVersion) < 0;
}

function readCheckoutPackageVersion(repoRoot) {
  try {
    const raw = fs.readFileSync(path.join(repoRoot, "webapp", "package.json"), "utf8");
    const pkg = JSON.parse(raw);
    return typeof pkg.version === "string" ? pkg.version : "";
  } catch {
    return "";
  }
}

function packagedUpdaterEnabled({ isPackaged, env = process.env }) {
  if (!isPackaged) return false;
  if (String(env.MARIONETTE_PACKAGED_UPDATER || "").trim() === "0") return false;
  return true;
}

/**
 * Merge a git-checkout check with a packaged-shell check into one banner payload.
 * Packaged shell skew (frozen app behind checkout) counts as available even when
 * git behind === 0, so users never see a false "up to date".
 */
function mergeUpdateAvailability({
  gitResult = {},
  packagedResult = null,
  isPackaged = false,
  shellVersion = "",
  checkoutVersion = "",
} = {}) {
  const gitAvailable = !!(gitResult && gitResult.available);
  const packagedAvailable = !!(packagedResult && packagedResult.available);
  const packagedDownloaded = !!(packagedResult && packagedResult.downloaded);
  const skew = isPackaged && shellBehindCheckout({ shellVersion, checkoutVersion });
  const available = gitAvailable || packagedAvailable || packagedDownloaded || skew;
  const latest =
    (packagedResult && packagedResult.latest) ||
    gitResult.latest ||
    checkoutVersion ||
    "";
  const installerUpdateRequired = !!(
    isPackaged && (packagedAvailable || packagedDownloaded || skew || gitResult.installerUpdateRequired)
  );
  return {
    ...gitResult,
    available,
    latest,
    downloaded: packagedDownloaded,
    busy: !!(packagedResult && packagedResult.busy),
    packagedAvailable,
    installerUpdateRequired,
    shellVersion: shellVersion || gitResult.currentVersion || "",
    checkoutVersion: checkoutVersion || "",
    source: packagedDownloaded
      ? "packaged-downloaded"
      : packagedAvailable
        ? "packaged"
        : skew
          ? "shell-skew"
          : gitAvailable
            ? "git"
            : "none",
  };
}

/**
 * Decide whether apply() should relaunch the current process after a successful
 * git source update. Packaged installs that still need a shell installer must
 * NOT relaunch -- that was the false-"updated" relaunch bug.
 */
function shouldRelaunchAfterSourceUpdate({ ok, isPackaged, installerUpdateRequired, packagedInstallPending }) {
  if (!ok) return false;
  if (packagedInstallPending) return false;
  if (isPackaged && installerUpdateRequired) return false;
  return true;
}

/**
 * Wire electron-updater. `createAutoUpdater` is injectable so unit tests never
 * load the real module (Electron APIs are missing under node:test).
 */
function registerPackagedUpdater(ipcMain, app, opts = {}) {
  const isPackaged = !!(app && app.isPackaged);
  const env = opts.env || process.env;
  if (!packagedUpdaterEnabled({ isPackaged, env })) {
    return {
      enabled: false,
      check: async () => ({ available: false, disabled: true }),
      downloadAndInstall: async () => ({ ok: false, error: "packaged updater disabled" }),
      isDownloaded: () => false,
    };
  }

  const broadcast = opts.broadcast || (() => {});
  const log = opts.log || ((line) => {
    try {
      require("node:fs").appendFileSync(
        require("node:path").join(require("node:os").homedir(), ".pmharness", "update.log"),
        `${new Date().toISOString()} [packaged] ${line}\n`,
      );
    } catch { /* logging must never break an update */ }
  });

  let autoUpdater;
  try {
    const factory = opts.createAutoUpdater || (() => require("electron-updater").autoUpdater);
    autoUpdater = factory();
  } catch (err) {
    log(`electron-updater unavailable: ${err && err.message ? err.message : err}`);
    return {
      enabled: false,
      check: async () => ({ available: false, error: "electron-updater unavailable" }),
      downloadAndInstall: async () => ({ ok: false, error: "electron-updater unavailable" }),
      isDownloaded: () => false,
    };
  }

  // Public GitHub Releases feed via app-update.yml baked by electron-builder.
  // Never call setFeedURL with a token; signed installs verify via code signature.
  autoUpdater.autoDownload = false;
  autoUpdater.autoInstallOnAppQuit = true;
  autoUpdater.allowDowngrade = false;
  autoUpdater.allowPrerelease = false;
  if (typeof autoUpdater.disableWebInstaller === "boolean") {
    // Prefer the native platform installer (dmg/zip/nsis/AppImage), not the
    // generic web installer path, so Gatekeeper / Authenticode checks apply.
    autoUpdater.disableWebInstaller = true;
  }

  let downloadedUpdate = null;
  let lastAvailable = null;
  let checking = false;
  let applying = false;

  const emitProgress = (payload) => {
    broadcast("updates:progress", payload);
  };

  /** Terminal signal: a packaged check finished with nothing actionable to show. */
  const emitCheckIdle = () => {
    emitProgress({ stage: "idle" });
  };

  autoUpdater.on("checking-for-update", () => {
    emitProgress({ stage: "check", message: "Checking for app shell update", percent: 0 });
  });
  autoUpdater.on("update-available", (info) => {
    const version = info && info.version ? String(info.version) : "";
    lastAvailable = { available: true, latest: version, info };
    broadcast("updates:available", {
      available: true,
      latest: version,
      downloaded: false,
      source: "packaged",
      installerUpdateRequired: true,
    });
    emitProgress({
      stage: "available",
      message: version ? `App shell v${version} is available` : "App shell update available",
      version,
      percent: 0,
    });
    log(`update-available ${version}`);
  });
  autoUpdater.on("update-not-available", (info) => {
    lastAvailable = { available: false, latest: info && info.version ? String(info.version) : "" };
    log("update-not-available");
    emitCheckIdle();
  });
  autoUpdater.on("download-progress", (p) => {
    const pct = p && typeof p.percent === "number" ? Math.round(p.percent) : null;
    emitProgress({
      stage: "download",
      message: pct != null ? `Downloading update ${pct}%` : "Downloading update",
      percent: pct,
      version: lastAvailable && lastAvailable.latest,
    });
  });
  autoUpdater.on("update-downloaded", (info) => {
    const version = info && info.version ? String(info.version) : (lastAvailable && lastAvailable.latest) || "";
    downloadedUpdate = info || { version };
    lastAvailable = { available: true, latest: version, downloaded: true, info };
    emitProgress({
      stage: "downloaded",
      message: version ? `App shell v${version} ready to install` : "App shell update ready to install",
      version,
      percent: 100,
    });
    broadcast("updates:available", {
      available: true,
      latest: version,
      downloaded: true,
      source: "packaged",
      installerUpdateRequired: true,
    });
    log(`update-downloaded ${version}`);
  });
  autoUpdater.on("error", (err) => {
    const message = String(err && err.message ? err.message : err || "packaged update failed");
    log(`error ${message}`);
    emitProgress({ stage: "error", message });
    if (checking) emitCheckIdle();
  });

  const check = async () => {
    if (checking) {
      return {
        available: !!(lastAvailable && lastAvailable.available),
        downloaded: !!downloadedUpdate,
        latest: (lastAvailable && lastAvailable.latest) || "",
        busy: true,
      };
    }
    checking = true;
    try {
      const result = await autoUpdater.checkForUpdates();
      const version =
        (result && result.updateInfo && result.updateInfo.version) ||
        (lastAvailable && lastAvailable.latest) ||
        "";
      const available = !!(lastAvailable && lastAvailable.available);
      return {
        available: available || !!downloadedUpdate,
        downloaded: !!downloadedUpdate,
        latest: version,
        updateInfo: result && result.updateInfo,
      };
    } catch (err) {
      const message = String(err && err.message ? err.message : err);
      log(`check failed: ${message}`);
      emitCheckIdle();
      return { available: false, error: message };
    } finally {
      checking = false;
    }
  };

  const downloadAndInstall = async () => {
    if (applying) return { ok: false, error: "a packaged update is already in progress" };
    applying = true;
    try {
      if (!downloadedUpdate) {
        emitProgress({ stage: "download", message: "Downloading app shell update", percent: 0 });
        // Ensure we know there is something to download.
        if (!(lastAvailable && lastAvailable.available)) {
          const checked = await check();
          if (!checked.available && !downloadedUpdate) {
            return { ok: false, error: checked.error || "no packaged update available" };
          }
        }
        await autoUpdater.downloadUpdate();
      }
      emitProgress({ stage: "install", message: "Installing app shell update", percent: 100 });
      // isSilent=false, isForceRunAfter=true: relaunch into the new shell.
      // Signed builds fail closed here rather than installing an unverified binary.
      setTimeout(() => {
        try {
          autoUpdater.quitAndInstall(false, true);
        } catch (err) {
          const message = String(err && err.message ? err.message : err);
          log(`quitAndInstall failed: ${message}`);
          emitProgress({ stage: "error", message });
        }
      }, 400);
      return { ok: true, installerUpdateRequired: true, packagedInstallPending: true };
    } catch (err) {
      const message = String(err && err.message ? err.message : err);
      log(`download/install failed: ${message}`);
      emitProgress({ stage: "error", message });
      return { ok: false, error: message };
    } finally {
      applying = false;
    }
  };

  // Optional background check ownership stays with the caller (update-bridge
  // watcher) so we never arm two independent timers that fight.
  if (ipcMain && typeof ipcMain.handle === "function") {
    ipcMain.handle("updates:packagedCheck", check);
    ipcMain.handle("updates:packagedInstall", downloadAndInstall);
  }

  return {
    enabled: true,
    check,
    downloadAndInstall,
    isDownloaded: () => !!downloadedUpdate,
    getLastAvailable: () => lastAvailable,
    github: { owner: GITHUB_OWNER, repo: GITHUB_REPO },
  };
}

module.exports = {
  GITHUB_OWNER,
  GITHUB_REPO,
  compareVersions,
  shellBehindCheckout,
  readCheckoutPackageVersion,
  packagedUpdaterEnabled,
  mergeUpdateAvailability,
  shouldRelaunchAfterSourceUpdate,
  registerPackagedUpdater,
};
