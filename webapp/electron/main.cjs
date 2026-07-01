// Electron main process for pm-harness.
// Responsibilities:
//  1. Spawn the Python harness backend (harness.cli gui) on a loopback port.
//  2. Create the BrowserWindow loading the Vite build (or dev server).
//  3. Register IPC handlers that back the renderer's transport seam
//     (window.harnessIPC.getJSON/postJSON/stream) + native fs/git bridges.
// The renderer is the SAME React app as the web build; only the transport
// implementation differs (IPC here vs fetch/SSE on the web).

const { app, BrowserWindow, ipcMain, dialog, shell, session } = require("electron");
app.name = "Marionette";
const { spawn } = require("node:child_process");
const http = require("node:http");
const path = require("node:path");
const net = require("node:net");
const fs = require("node:fs");
const os = require("node:os");
const { readLiveUpdateMarker } = require("./update-marker.cjs");

const isDev = !!process.env.PMHARNESS_DEV_SERVER;

// Persistent main-process log, shared with the backend [out]/[err] lines under
// ~/.pmharness/electron.log so a death is always diagnosable after the fact.
function logMain(msg) {
  try {
    fs.appendFileSync(
      path.join(os.homedir(), ".pmharness", "electron.log"),
      `${new Date().toISOString()} ${msg}\n`
    );
  } catch { /* logging must never throw */ }
}

// Safety net. A stray async throw (or a send() on a renderer torn down mid-stream)
// must NOT take down the whole app: previously an uncaught exception in main
// exited the process, orphaned the backend, and the renderer then reconnected to
// a dead port -- so the LLM stream, CodeGraph, wiki, and terminal all went dark at
// once. Log loudly and stay alive instead of crashing.
process.on("uncaughtException", (err) => {
  logMain(`[uncaughtException] ${err && err.stack ? err.stack : err}`);
});
process.on("unhandledRejection", (reason) => {
  logMain(`[unhandledRejection] ${reason && reason.stack ? reason.stack : reason}`);
});

// One running instance per machine. A second launch (double-click, Dock, or a
// checkout's start.sh on top of the installed app) otherwise spawns a SECOND
// backend on a different port; the two fight over the marker and the live
// renderer's connections get pulled out from under it -- observed as a mid-session
// respawn that kills graph/wiki/terminal at once. Hand focus to the first instance.
const gotSingleInstanceLock = isDev || app.requestSingleInstanceLock();
if (!gotSingleInstanceLock) {
  app.quit();
} else {
  app.on("second-instance", () => {
    const w = BrowserWindow.getAllWindows()[0];
    if (w) { try { if (w.isMinimized()) w.restore(); w.show(); w.focus(); } catch { /* ignore */ } }
  });
}

// ---- login-shell environment capture (macOS Finder/Dock launch fix) --------
// When a packaged app is launched from Finder/Dock (not a terminal), macOS gives
// it a MINIMAL launchd environment: it is missing the user's real PATH, their
// ssh-agent socket (SSH_AUTH_SOCK), and anything set in ~/.zprofile/.zshrc/etc.
// That is exactly why `ssh <host>` (and tools resolved off PATH) behave
// differently inside the app than in a real terminal -- the agent keys and
// ~/.ssh host aliases resolve against a stripped env. We fix this the same way
// VS Code / Hyper do: run the user's LOGIN+INTERACTIVE shell once, dump its
// environment, and merge the missing vars in. Cached for the process lifetime.
let _shellEnvCache = null;
function loginShellEnv() {
  if (_shellEnvCache !== null) return _shellEnvCache;
  _shellEnvCache = {};
  // Only needed on macOS/Linux GUI launches; on Windows the env is already full.
  if (process.platform === "win32") return _shellEnvCache;
  try {
    const { execFileSync } = require("node:child_process");
    const shellPath = process.env.SHELL || "/bin/zsh";
    // A unique marker brackets the `env` dump so we can parse it cleanly even if
    // the user's rc files print banners. -l (login) + -i (interactive) so
    // ~/.zprofile AND ~/.zshrc both run, matching a real terminal.
    const marker = "__PMH_ENV_" + Date.now() + "__";
    const script = `printf '%s\n' '${marker}'; /usr/bin/env; printf '%s\n' '${marker}'`;
    const out = execFileSync(shellPath, ["-l", "-i", "-c", script], {
      encoding: "utf8",
      timeout: 5000,
      stdio: ["ignore", "pipe", "ignore"],
    });
    const parts = out.split(marker);
    if (parts.length >= 3) {
      const body = parts[1];
      for (const line of body.split("\n")) {
        const eq = line.indexOf("=");
        if (eq <= 0) continue;
        const key = line.slice(0, eq);
        const val = line.slice(eq + 1);
        if (key) _shellEnvCache[key] = val;
      }
    }
  } catch (e) {
    // Any failure -> empty merge; the app still works with the launchd env.
    _shellEnvCache = {};
  }
  return _shellEnvCache;
}

let backend = null;
let backendPort = 8799;
let win = null;

// The source checkout the app runs from. The backend is `harness.cli` under this
// root, and the self-updater pulls + rebuilds it in place. HARNESS_REPO wins;
// otherwise a packaged shell assumes ~/pm-harness and a dev run resolves the repo
// two levels up from webapp/electron/.
function resolveRepoRoot() {
  return (
    process.env.HARNESS_REPO ||
    (app.isPackaged ? path.join(os.homedir(), "pm-harness") : path.resolve(__dirname, "..", ".."))
  );
}

function freePort() {
  return new Promise((resolve) => {
    const srv = net.createServer();
    srv.listen(0, "127.0.0.1", () => {
      const p = srv.address().port;
      srv.close(() => resolve(p));
    });
  });
}

function waitForBackend(port, timeoutMs = 20000) {
  const start = Date.now();
  return new Promise((resolve, reject) => {
    const probe = () => {
      const req = http.get({ host: "127.0.0.1", port, path: "/api/config", timeout: 2000 }, (res) => {
        res.destroy();
        resolve(true);
      });
      req.on("error", () => {
        if (Date.now() - start > timeoutMs) return reject(new Error("backend did not start"));
        setTimeout(probe, 300);
      });
      req.on("timeout", () => { req.destroy(); });
    };
    probe();
  });
}

// Single-backend-per-machine: a marker file records the live backend port so a
// second window REUSES it instead of spawning another process on the same SQLite
// state (which causes "database is locked"). The marker is validated by a health
// probe before reuse; stale markers are ignored.
function markerPath() {
  const dir = path.join(os.homedir(), ".pmharness");
  try { fs.mkdirSync(dir, { recursive: true }); } catch {}
  return path.join(dir, "backend.json");
}

async function startBackend() {
  // 1. Try to reuse an existing healthy backend.
  try {
    const m = JSON.parse(fs.readFileSync(markerPath(), "utf8"));
    if (m && m.port) {
      await waitForBackend(m.port, 2000);
      backendPort = m.port;
      backend = null; // not ours to kill
      console.log(`[backend] reusing existing backend on ${backendPort}`);
      return;
    }
  } catch {}

  // 1b. If a self-update is applying (git pull + rebuild), do NOT spawn a fresh
  // backend against the same state -- park until the update finishes or its
  // marker goes stale, so a mid-update relaunch can't race the rebuild.
  for (let i = 0; i < 40; i++) {
    const live = readLiveUpdateMarker(path.join(os.homedir(), ".pmharness"));
    if (!live) break;
    console.log(`[backend] update in progress (pid ${live.pid}); parking...`);
    await new Promise((r) => setTimeout(r, 500));
  }

  // 2. Spawn a fresh backend on a free port and record the marker.
  backendPort = await freePort();
  // Backend resolution: the source checkout the app runs from (see
  // resolveRepoRoot). A fully self-contained bundle (PyInstaller) is the
  // distribution path; for a source/checkout run we use the repo's venv.
  const repoRoot = resolveRepoRoot();

  let binaryPath = null;
  if (app.isPackaged && process.resourcesPath) {
    const p = path.join(process.resourcesPath, "pmharness-backend");
    if (fs.existsSync(p)) {
      binaryPath = p;
    }
  }

  const _dbg = (msg) => { try { fs.appendFileSync(path.join(os.homedir(), ".pmharness", "electron.log"), `${new Date().toISOString()} ${msg}\n`); } catch {} };

  // Merge the user's real login-shell environment UNDER process.env so the
  // backend (and every run_command it spawns -- ssh, git, etc.) sees the same
  // PATH, ssh-agent socket, and profile vars it would in a terminal. process.env
  // still wins for anything the app set deliberately. Only fills in when a GUI
  // launch left those vars stripped.
  const _shellEnv = app.isPackaged ? loginShellEnv() : {};
  // PYTHONUNBUFFERED: stream backend stdout/stderr to the log immediately instead
  // of sitting in a pipe buffer (that buffering hid the real startup/crash lines
  // and left a ~20-min gap between "spawning" and "GUI on" in the log).
  const customEnv = { ..._shellEnv, ...process.env, PYTHONUNBUFFERED: "1", HARNESS_REPO: process.env.HARNESS_REPO || repoRoot };

  // codegraph self-containment: only safe with a REAL bundled `node` binary.
  // The electron-as-node trick (ELECTRON_RUN_AS_NODE) does NOT work for codegraph because
  // codegraph uses worker_threads (new Worker), which re-launch the Electron binary and recurse
  // infinitely (verified 2026-06-26). So we ONLY inject codegraph/node shims when a bundled real
  // node binary is shipped at Resources/node/bin/node; otherwise we leave PATH alone so a system
  // codegraph/node (dev machines) keeps working. See .hermes/plans node-bundling verdict.
  try {
    const bundledNode = app.isPackaged
      ? path.join(process.resourcesPath, "node", "bin", "node")
      : "";
    let codegraphJsPath = "";
    if (app.isPackaged) {
      const cg = path.join(process.resourcesPath, "codegraph", "dist", "bin", "codegraph.js");
      if (fs.existsSync(cg)) codegraphJsPath = cg;
    }
    if (bundledNode && fs.existsSync(bundledNode) && codegraphJsPath) {
      const shimDir = path.join(app.getPath("userData"), "bin");
      fs.mkdirSync(shimDir, { recursive: true });
      const codegraphShimPath = path.join(shimDir, "codegraph");
      fs.writeFileSync(codegraphShimPath,
        `#!/bin/sh\nexec "${bundledNode}" "${codegraphJsPath}" "$@"\n`, "utf8");
      fs.chmodSync(codegraphShimPath, 0o755);
      const nodeShimPath = path.join(shimDir, "node");
      fs.writeFileSync(nodeShimPath, `#!/bin/sh\nexec "${bundledNode}" "$@"\n`, "utf8");
      fs.chmodSync(nodeShimPath, 0o755);
      customEnv.PATH = shimDir + path.delimiter + (customEnv.PATH || process.env.PATH || "");
      customEnv.PUPPETMASTER_CODEGRAPH_NO_NPX = "1";
      _dbg(`codegraph self-contained via bundled node at ${bundledNode}`);
    } else {
      _dbg("No bundled node binary; using system codegraph/node if present (no shim).");
    }
  } catch (e) {
    _dbg(`codegraph shim setup skipped: ${e}`);
  }

  if (binaryPath) {
    _dbg(`spawning bundled binary: ${binaryPath} cwd=${repoRoot} port=${backendPort} packaged=${app.isPackaged}`);
    backend = spawn(binaryPath, ["gui", "--port", String(backendPort)], {
      cwd: repoRoot,
      env: customEnv,
      stdio: ["ignore", "pipe", "pipe"],
    });
  } else {
    const py = process.env.PMHARNESS_PYTHON || path.join(repoRoot, ".venv", "bin", "python");
    _dbg(`spawning python backend: ${py} cwd=${repoRoot} port=${backendPort} packaged=${app.isPackaged}`);
    backend = spawn(py, ["-m", "harness.cli", "gui", "--port", String(backendPort)], {
      cwd: repoRoot,
      env: customEnv,
      stdio: ["ignore", "pipe", "pipe"],
    });
  }

  backend.on("error", (e) => _dbg(`spawn error: ${e.message}`));
  // Log unexpected backend death (code/signal) instead of silently losing it; the
  // renderer surfaces graph/wiki/terminal failures on its own, but this pins the
  // cause in the log. cleanupBackend() nulls `backend` on intentional teardown, so
  // a non-null ref here means the exit was NOT us.
  backend.on("exit", (code, signal) => {
    if (backend) _dbg(`[backend EXITED unexpectedly] code=${code} signal=${signal}`);
  });
  backend.stdout.on("data", (d) => { _dbg(`[out] ${d}`); process.stdout.write(`[backend] ${d}`); });
  backend.stderr.on("data", (d) => { _dbg(`[err] ${d}`); process.stderr.write(`[backend] ${d}`); });
  await waitForBackend(backendPort);
  try { fs.writeFileSync(markerPath(), JSON.stringify({ port: backendPort, pid: backend.pid, at: Date.now() })); } catch {}
}

// ---- transport seam over IPC: proxy to the local backend ----
function authToken() {
  try { return fs.readFileSync(path.join(os.homedir(), ".pmharness", "token"), "utf8").trim(); }
  catch { return ""; }
}

function backendRequest(method, apiPath, body) {
  return new Promise((resolve, reject) => {
    const data = body ? JSON.stringify(body) : null;
    const req = http.request({
      host: "127.0.0.1", port: backendPort, path: apiPath, method,
      headers: { "Content-Type": "application/json", "X-Harness-Token": authToken(), ...(data ? { "Content-Length": Buffer.byteLength(data) } : {}) },
    }, (res) => {
      let buf = "";
      res.on("data", (c) => (buf += c));
      res.on("end", () => { try { resolve(JSON.parse(buf || "null")); } catch { resolve(null); } });
    });
    req.on("error", reject);
    if (data) req.write(data);
    req.end();
  });
}

ipcMain.handle("harness:getJSON", (_e, p) => backendRequest("GET", p));
ipcMain.handle("harness:postJSON", (_e, p, body) => backendRequest("POST", p, body));

// Image upload bridge: the renderer hands us raw bytes (File over IPC can't carry
// a browser File object), we POST a multipart body to the backend's /api/upload on
// the loopback port so the saved path matches what the chat/view_image path reads.
// Without this, transport.uploadFile fell back to a bare fetch("/api/upload") which
// has no backend origin in the packaged app -> "Image upload failed".
ipcMain.handle("harness:uploadFile", async (_e, payload) => {
  try {
    const { name, type, bytes } = payload || {};
    if (!bytes) return [];
    const buf = Buffer.from(bytes); // bytes arrives as an ArrayBuffer/Uint8Array
    const safeName = (name && String(name)) || `image-${Date.now()}.png`;
    const boundary = "----MarionetteUpload" + Math.random().toString(16).slice(2);
    const head = Buffer.from(
      `--${boundary}\r\n` +
      `Content-Disposition: form-data; name="file"; filename="${safeName.replace(/"/g, "")}"\r\n` +
      `Content-Type: ${type || "application/octet-stream"}\r\n\r\n`
    );
    const tail = Buffer.from(`\r\n--${boundary}--\r\n`);
    const body = Buffer.concat([head, buf, tail]);
    return await new Promise((resolve) => {
      const req = http.request({
        host: "127.0.0.1", port: backendPort, path: "/api/upload", method: "POST",
        headers: {
          "Content-Type": `multipart/form-data; boundary=${boundary}`,
          "Content-Length": body.length,
          "X-Harness-Token": authToken(),
        },
      }, (res) => {
        let b = "";
        res.on("data", (c) => (b += c));
        res.on("end", () => {
          try { resolve(JSON.parse(b || "{}").saved || []); }
          catch { resolve([]); }
        });
      });
      req.on("error", () => resolve([]));
      req.write(body);
      req.end();
    });
  } catch {
    return [];
  }
});

// Native folder picker (Cursor-style "Open Folder"). Returns absolute path or null.
ipcMain.handle("harness:pickFolder", async () => {
  const res = await dialog.showOpenDialog({ properties: ["openDirectory", "createDirectory"] });
  if (res.canceled || !res.filePaths || !res.filePaths.length) return null;
  return res.filePaths[0];
});

// SSE stream: bridge backend EventSource-style stream to renderer via events.
//
// Robustness: every event.sender.send() is guarded. When the user stops + swaps
// the model + resends, the renderer tears down the old stream's webContents
// mid-flight; an unguarded send() on a destroyed sender throws "Object has been
// destroyed" -- an UNCAUGHT exception in the Electron main process, which exits
// the whole app (backend orphaned -> respawn on a new port -> ECONNREFUSED ->
// everything dead). We also always abort the upstream backend request and remove
// the one-shot cancel listener so connections + listeners never leak.
ipcMain.on("harness:stream", (event, channelId, apiPath) => {
  const tok = authToken();
  const streamPath = tok ? apiPath + (apiPath.includes("?") ? "&" : "?") + "token=" + encodeURIComponent(tok) : apiPath;
  let req = null;
  let finished = false;

  // Safe send: never throw if the renderer (webContents) is gone.
  const safeSend = (channel, payload) => {
    try {
      if (event.sender && !event.sender.isDestroyed()) {
        event.sender.send(channel, payload);
      }
    } catch {
      // sender destroyed between the check and the send -- swallow.
    }
  };

  const cleanup = () => {
    if (finished) return;
    finished = true;
    try { ipcMain.removeListener(`${channelId}:cancel`, onCancel); } catch {}
    try { if (req) req.destroy(); } catch {}
  };

  const onCancel = () => { cleanup(); };

  req = http.get({ host: "127.0.0.1", port: backendPort, path: streamPath }, (res) => {
    res.setEncoding("utf8");
    let buf = "";
    res.on("data", (chunk) => {
      buf += chunk;
      let idx;
      while ((idx = buf.indexOf("\n\n")) >= 0) {
        const frame = buf.slice(0, idx); buf = buf.slice(idx + 2);
        const line = frame.split("\n").find((l) => l.startsWith("data: "));
        if (!line) continue;
        const payload = line.slice(6);
        try {
          const ev = JSON.parse(payload);
          if (ev.kind === "done") { safeSend(`${channelId}:done`); res.destroy(); cleanup(); return; }
          safeSend(`${channelId}:event`, ev);
        } catch {}
      }
    });
    res.on("end", () => { safeSend(`${channelId}:done`); cleanup(); });
    res.on("error", (e) => { safeSend(`${channelId}:error`, String(e)); cleanup(); });
  });
  req.on("error", (e) => { safeSend(`${channelId}:error`, String(e)); cleanup(); });
  ipcMain.once(`${channelId}:cancel`, onCancel);
});

// ---- native bridges (file tree + git) ----
const { registerFsBridge } = require("./fs-bridge.cjs");
const { registerGitBridge } = require("./git-bridge.cjs");
const { registerUpdateBridge } = require("./update-bridge.cjs");
const { registerAutoUpdater } = require("./auto-updater.cjs");
registerFsBridge(ipcMain);
registerGitBridge(ipcMain);
// Two delivery models behind one IPC surface (StatusBar's update pill):
//   - Installed .app: swap the whole signed bundle via electron-updater. New
//     GitHub releases download in the background and apply on relaunch -- the
//     "install once, updates just land" path for everyone on the DMG.
//   - Git checkout (contributors): pull + rebuild the source in place, then
//     relaunch. Working from source has no bundle to swap.
if (app.isPackaged) {
  registerAutoUpdater(ipcMain, app, shell, {
    cleanup: () => { try { cleanupBackend(); } catch { /* ignore */ } },
  });
} else {
  registerUpdateBridge(ipcMain, app, shell, {
    getRepoRoot: resolveRepoRoot,
    relaunch: () => {
      try { cleanupBackend(); } catch { /* ignore */ }
      app.relaunch();
      app.exit(0);
    },
  });
}

function createWindow() {
  win = new BrowserWindow({
    width: 1440, height: 900, backgroundColor: "#0f1113",
    titleBarStyle: "hiddenInset",
    webPreferences: {
      preload: path.join(__dirname, "preload.cjs"),
      contextIsolation: true,
      webviewTag: true,   // enables the real in-app browser
    },
  });
  // expose the backend port to the renderer for any direct needs
  win.webContents.on("did-finish-load", () => {
    let tok = "";
    try { tok = fs.readFileSync(path.join(os.homedir(), ".pmharness", "token"), "utf8").trim(); } catch {}
    win.webContents.executeJavaScript(
      `window.__HARNESS_PORT__=${backendPort};window.__HARNESS_TOKEN__=${JSON.stringify(tok)};`
    ).catch(() => {});
  });
  if (isDev) win.loadURL(process.env.PMHARNESS_DEV_SERVER);
  else win.loadFile(path.join(__dirname, "..", "dist", "index.html"));

  // Drop the reference when the window is closed so a reopen builds a clean one
  // (and a failed renderer load doesn't leave a half-dead window bound to `win`).
  win.on("closed", () => { win = null; });
  // If the renderer fails to load (white screen / error), reload it once so a
  // transient failure on reopen self-heals instead of stranding the user.
  win.webContents.on("did-fail-load", (_e, errorCode, errorDesc, validatedURL, isMainFrame) => {
    if (isMainFrame && errorCode !== -3) {  // -3 = aborted (navigation), ignore
      _dbg2(`renderer did-fail-load ${errorCode} ${errorDesc} ${validatedURL}`);
      setTimeout(() => {
        try {
          if (isDev) win && win.loadURL(process.env.PMHARNESS_DEV_SERVER);
          else win && win.loadFile(path.join(__dirname, "..", "dist", "index.html"));
        } catch {}
      }, 500);
    }
  });
}

// Configure the in-app browser's PERSISTENT session partition. The <webview>
// uses partition="persist:browser"; here we give that session a realistic
// desktop user-agent (some sites -- X/Twitter included -- refuse to keep a
// session alive for the default Electron UA and bounce you back to login) and
// route webview popups (OAuth/login windows) to a real child window in the SAME
// partition so the auth cookie is written to the session the webview reads from.
function configureBrowserSession() {
  try {
    const ses = session.fromPartition("persist:browser");
    // A mainstream Chrome UA so login flows behave like a normal browser.
    const chromeUA =
      "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 " +
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36";
    try { ses.setUserAgent(chromeUA); } catch {}
  } catch (e) {
    _dbg2(`browser session config failed: ${e}`);
  }
}

function _dbg2(msg) {
  try { fs.appendFileSync(path.join(os.homedir(), ".pmharness", "electron.log"), `${new Date().toISOString()} ${msg}\n`); } catch {}
}

// When the in-app browser opens a popup (window.open from an OAuth/login page),
// give it a real BrowserWindow bound to the SAME persistent partition so the
// login completes and its cookie lands in the session the webview shares.
app.on("web-contents-created", (_e, contents) => {
  if (contents.getType() === "webview") {
    contents.setWindowOpenHandler(({ url }) => {
      return {
        action: "allow",
        overrideBrowserWindowOptions: {
          webPreferences: { partition: "persist:browser", contextIsolation: true },
          width: 600,
          height: 750,
        },
      };
    });
  }
});

app.whenReady().then(async () => {
  if (!gotSingleInstanceLock) return; // a prior instance owns the backend
  configureBrowserSession();
  try { await startBackend(); } catch (e) { console.error("backend start failed:", e); }
  createWindow();
  // Re-open: ensure a healthy backend, THEN (re)create the window. startBackend()
  // is idempotent -- it reuses a live backend via the marker, or respawns one if
  // it died -- so a reopened window always connects to a working backend.
  app.on("activate", async () => {
    if (BrowserWindow.getAllWindows().length === 0) {
      try { await startBackend(); } catch (e) { console.error("backend re-ensure failed:", e); }
      createWindow();
    } else {
      // A window exists but may be hidden/behind -- surface it.
      const w = BrowserWindow.getAllWindows()[0];
      try { if (w.isMinimized()) w.restore(); w.show(); w.focus(); } catch {}
    }
  });
});

function cleanupBackend() {
  if (backend) {
    try { fs.unlinkSync(markerPath()); } catch {}
    backend.kill();
    backend = null;
  }
}
app.on("window-all-closed", () => {
  // On macOS the app stays alive when all windows close (standard behavior), so
  // we MUST keep the backend running -- otherwise reopening a window (Cmd/Ctrl+W
  // then reopen, or Dock click) loads a renderer against a dead backend and every
  // API call errors. The backend is torn down only on a real quit (before-quit).
  if (process.platform !== "darwin") {
    cleanupBackend();
    app.quit();
  }
});
app.on("before-quit", cleanupBackend);
