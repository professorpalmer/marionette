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
const crypto = require("node:crypto");
const { readLiveUpdateMarker } = require("./update-marker.cjs");
const { isInstallComplete, runBootstrap, reinjectPortableTools } = require("./bootstrap.cjs");

// Must run before any git/npm/uv child spawns: on Windows the portable tools
// installed by first-run bootstrap are only on PATH in-memory, per process.
reinjectPortableTools();

const isDev = !!process.env.PMHARNESS_DEV_SERVER;
const isPackaged = app.isPackaged;

function pmharnessHome() {
  return path.join(os.homedir(), ".pmharness");
}

// server.py anchors HARNESS_STATE_DIR to ~/.pmharness/state when unset and writes
// token, backend.json, workspace.json, etc. there. Older installs used flat
// ~/.pmharness/*. Prefer state/ on read (state first, then legacy) so a second
// window reusing a live backend adopts the same files the server wrote.
function pmharnessStateDir() {
  return path.join(pmharnessHome(), "state");
}

function readPmHarnessStateFile(name) {
  for (const dir of [pmharnessStateDir(), pmharnessHome()]) {
    try {
      return fs.readFileSync(path.join(dir, name), "utf8");
    } catch {}
  }
  return null;
}

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
let quitting = false;
// Self-dev Vite dev server: when Live Self-Editing is on, we serve the React UI
// from a Vite dev server (real HMR) instead of the prebuilt dist/, so edits to
// webapp/src/** are live with no rebuild/restart. Null until started.
let viteProc = null;
let viteUrl = null;
// Instance-local auth token, minted by main and handed to BOTH the backend (via
// HARNESS_TOKEN env) and the renderer. Previously each backend generated its own
// token and wrote it to a SHARED ~/.pmharness/token file (last-writer-wins). When
// an update relaunch (or a crash) left a stale backend alive on the old port, the
// shared file no longer matched the backend the renderer was actually talking to,
// so every request 403'd and the whole UI read as "disconnected". Owning the
// token here makes renderer<->backend agree by construction, independent of the
// shared file and any stale second instance. The reuse path (below) adopts the
// running backend's token instead of this freshly-minted one.
let harnessToken = crypto.randomBytes(16).toString("hex");
// Timestamps of recent unexpected respawns -- caps a crash loop (see backend.on exit).
let respawnTimes = [];
// Coalesces concurrent startBackend() calls. Two overlapping starts (app 'ready'
// racing 'activate', or a respawn racing a reopen) each spawned a backend on a
// fresh port before either marker was written -- two processes then hit the same
// Puppetmaster SQLite and one died with "database is locked", disconnecting the
// UI. A single in-flight promise guarantees at most one backend launch at a time.
let startInFlight = null;

// The source checkout the app runs from. Marionette always runs from source
// (Hermes model): the backend is `harness.cli` under this root, and the updater
// pulls + rebuilds it in place. HARNESS_REPO wins; in a packaged thin shell the
// checkout lives at ~/.marionette/marionette (bootstrapped on first launch); in
// dev the repo is two levels up from webapp/electron/.
function packagedRepoRoot() {
  return path.join(os.homedir(), ".marionette", "marionette");
}

function resolveRepoRoot() {
  if (process.env.HARNESS_REPO) return process.env.HARNESS_REPO;
  if (isPackaged) return packagedRepoRoot();
  return path.resolve(__dirname, "..", "..");
}

function venvPython(repoRoot) {
  return process.platform === "win32"
    ? path.join(repoRoot, ".venv", "Scripts", "python.exe")
    : path.join(repoRoot, ".venv", "bin", "python");
}

// Live-UI mode: serve the React renderer from a Vite HMR dev server instead of
// the prebuilt dist/, so edits to webapp/src/** are live with no rebuild. The
// backend already always runs from the editable source checkout (Marionette is
// source-run), so this toggle governs the UI only. Sourced from MARIONETTE_SELF_DEV
// env or ~/.pmharness/self-dev.json so the UI can toggle it durably. Persisted in
// userData-adjacent state, not the repo.
function selfDevConfigPath() {
  return path.join(os.homedir(), ".pmharness", "self-dev.json");
}
function selfDevEnabled() {
  const env = String(process.env.MARIONETTE_SELF_DEV || "").toLowerCase();
  if (env === "1" || env === "true" || env === "yes") return true;
  if (env === "0" || env === "false" || env === "no") return false;
  try {
    const j = JSON.parse(fs.readFileSync(selfDevConfigPath(), "utf8"));
    return !!(j && j.enabled);
  } catch { return false; }
}
function setSelfDevEnabled(enabled) {
  try {
    fs.mkdirSync(path.dirname(selfDevConfigPath()), { recursive: true });
    fs.writeFileSync(selfDevConfigPath(), JSON.stringify({ enabled: !!enabled }, null, 2));
    return true;
  } catch { return false; }
}

// Live React needs the checkout's webapp with installed deps (the Vite binary).
function viteDevViable(repoRoot) {
  try {
    return fs.existsSync(path.join(repoRoot, "webapp", "node_modules", ".bin", "vite")) &&
           fs.existsSync(path.join(repoRoot, "webapp", "src"));
  } catch { return false; }
}

// Whether the renderer should be served from the Vite HMR dev server (live React)
// rather than the prebuilt dist/. Only when Live UI is toggled on and the webapp
// checkout is usable; never in the classic dev flow (PMHARNESS_DEV_SERVER already
// owns it).
function shouldUseViteDev(repoRoot) {
  return !isDev && selfDevEnabled() && viteDevViable(repoRoot);
}

// Start (or reuse) a Vite dev server for the editable webapp. Returns its URL, or
// null if it can't start -- callers fall back to loadFile(dist). The renderer
// talks to the backend over IPC (window.harnessIPC), so Vite's /api proxy is
// irrelevant here; we only need the HMR-served page.
async function ensureViteDevServer(repoRoot) {
  if (viteUrl && viteProc && viteProc.exitCode === null) return viteUrl;
  if (!viteDevViable(repoRoot)) return null;
  const webappDir = path.join(repoRoot, "webapp");
  const viteBin = path.join(webappDir, "node_modules", ".bin",
    process.platform === "win32" ? "vite.cmd" : "vite");
  const port = await freePort();
  const url = `http://127.0.0.1:${port}`;
  try {
    viteProc = spawn(viteBin, ["--host", "127.0.0.1", "--port", String(port), "--strictPort", "--clearScreen", "false"], {
      cwd: webappDir,
      env: { ...process.env },
      stdio: ["ignore", "pipe", "pipe"],
      windowsHide: true,
      // .cmd shims on Windows only spawn through a shell (Node CVE-2024-27980 guard)
      shell: process.platform === "win32",
    });
    viteProc.stdout.on("data", (d) => _dbg2(`[vite] ${d}`));
    viteProc.stderr.on("data", (d) => _dbg2(`[vite:err] ${d}`));
    viteProc.on("exit", (code) => { _dbg2(`[vite] exited code=${code}`); viteProc = null; viteUrl = null; });
  } catch (e) {
    _dbg2(`[vite] spawn failed: ${e && e.message}`);
    viteProc = null;
    return null;
  }
  // Wait for the dev server to answer before we point the window at it.
  const ready = await new Promise((resolve) => {
    const start = Date.now();
    const probe = () => {
      const req = http.get({ host: "127.0.0.1", port, path: "/", timeout: 1500 }, (res) => {
        res.destroy();
        resolve(true);
      });
      req.on("error", () => {
        if (Date.now() - start > 20000) return resolve(false);
        setTimeout(probe, 300);
      });
      req.on("timeout", () => { req.destroy(); });
    };
    probe();
  });
  if (!ready) {
    _dbg2("[vite] dev server did not become ready; falling back to dist");
    cleanupVite();
    return null;
  }
  viteUrl = url;
  _dbg2(`[vite] dev server ready at ${url} (live React HMR)`);
  return viteUrl;
}

function cleanupVite() {
  if (viteProc) {
    try { viteProc.kill(); } catch { /* already gone */ }
    viteProc = null;
  }
  viteUrl = null;
}

// Point the window at the right renderer source: the classic dev server, the
// self-dev Vite HMR server (live React), or the prebuilt dist/ bundle.
async function loadRenderer() {
  if (!win) return;
  if (isDev) { win.loadURL(process.env.PMHARNESS_DEV_SERVER); return; }
  if (shouldUseViteDev(resolveRepoRoot())) {
    const url = await ensureViteDevServer(resolveRepoRoot());
    if (url) { win.loadURL(url); return; }
  } else {
    cleanupVite();  // self-dev turned off -> stop the dev server
  }
  win.loadFile(resolveDistIndex());
}

// Resolve which built renderer to load. The packaged app normally serves the
// dist baked into app.asar (path.join(__dirname, "..", "dist")), which is FROZEN
// at build time -- so a `npm run build` in the editable checkout (where the
// backend runs from) never shows up, and UI edits appear stale until an
// Update & Relaunch or a manual asar repack. Marionette is source-run, so prefer
// the checkout's freshly-built webapp/dist/index.html when it exists; fall back
// to the bundled dist otherwise. This makes `npm run build` the source of truth
// for the UI on the next relaunch, matching how backend edits already go live.
function resolveDistIndex() {
  const bundled = path.join(__dirname, "..", "dist", "index.html");
  try {
    const checkoutDist = path.join(resolveRepoRoot(), "webapp", "dist", "index.html");
    // Prefer the checkout build WHENEVER it exists and is non-empty. Its mere
    // existence means the user ran `npm run build` in the source checkout (where
    // the backend runs from), which is the explicit signal that the checkout is
    // the UI source of truth. We deliberately do NOT compare mtimes: an asar
    // repack stamps the bundled index.html with "now", which would spuriously
    // out-date a valid fresh checkout build and pin the UI to the stale bundled
    // dist -- the exact bug that made rebuilt Steer/Queue UI never appear.
    if (fs.existsSync(checkoutDist) && checkoutDist !== bundled) {
      let ok = false;
      try { ok = fs.statSync(checkoutDist).size > 0; } catch { ok = false; }
      if (ok) {
        _dbg2(`loadRenderer: using checkout dist (${checkoutDist})`);
        return checkoutDist;
      }
    }
  } catch (e) {
    try { _dbg2(`resolveDistIndex fallback: ${e && e.message ? e.message : e}`); } catch {}
  }
  return bundled;
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
  const dir = pmharnessStateDir();
  try { fs.mkdirSync(dir, { recursive: true }); } catch {}
  return path.join(dir, "backend.json");
}

function unlinkMarker() {
  for (const p of [markerPath(), path.join(pmharnessHome(), "backend.json")]) {
    try { fs.unlinkSync(p); } catch {}
  }
}

function startBackend() {
  // Coalesce overlapping starts onto one in-flight promise so we never launch a
  // second backend against the same SQLite while the first is still starting up.
  if (startInFlight) return startInFlight;
  startInFlight = _startBackendOnce().finally(() => { startInFlight = null; });
  return startInFlight;
}

async function _startBackendOnce() {
  // 1. Try to reuse an existing healthy backend.
  try {
    const raw = readPmHarnessStateFile("backend.json");
    const m = raw ? JSON.parse(raw) : null;
    if (m && m.port) {
      await waitForBackend(m.port, 2000);
      backendPort = m.port;
      backend = null; // not ours to kill
      // Adopt the running backend's token (minted by whichever main spawned it)
      // so our renderer/IPC authenticate against IT rather than our own unused
      // freshly-minted token.
      const t = readPmHarnessStateFile("token");
      if (t && t.trim()) harnessToken = t.trim();
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
  // resolveRepoRoot). Marionette always runs the backend from the repo's venv --
  // `python -m harness.cli` -- so self-edits go live on the next restart.
  const repoRoot = resolveRepoRoot();

  const _dbg = (msg) => { try { fs.appendFileSync(path.join(os.homedir(), ".pmharness", "electron.log"), `${new Date().toISOString()} ${msg}\n`); } catch {} };

  // Merge the user's real login-shell environment UNDER process.env so the
  // backend (and every run_command it spawns -- ssh, git, etc.) sees the same
  // PATH, ssh-agent socket, and profile vars it would in a terminal. process.env
  // still wins for anything the app set deliberately. A GUI launch (Dock/Finder
  // or the `marionette` launcher) gets a stripped launchd env, so we fill it in
  // for every non-dev run; classic terminal dev (PMHARNESS_DEV_SERVER) already
  // has a full env, so we skip the extra login-shell spawn there.
  const _shellEnv = isDev ? {} : loginShellEnv();
  // PYTHONUNBUFFERED: stream backend stdout/stderr to the log immediately instead
  // of sitting in a pipe buffer (that buffering hid the real startup/crash lines
  // and left a ~20-min gap between "spawning" and "GUI on" in the log).
  const customEnv = { ..._shellEnv, ...process.env, PYTHONUNBUFFERED: "1", HARNESS_REPO: process.env.HARNESS_REPO || repoRoot, HARNESS_TOKEN: harnessToken };

  // Point PMHARNESS_PYTHON at the checkout's venv interpreter for dispatching
  // Puppetmaster / implement workers. The venv has editable harness + puppetmaster
  // (the live source); the backend's resolver validates puppetmaster-importability
  // before trusting it.
  if (!customEnv.PMHARNESS_PYTHON) {
    const venvPy = venvPython(repoRoot);
    if (fs.existsSync(venvPy)) customEnv.PMHARNESS_PYTHON = venvPy;
  }

  // CodeGraph runs off the system `node` + the `codegraph` binary from the
  // installed Puppetmaster (the installer ensures both). No bundled-node shim is
  // needed in the source-run model.

  const py = process.env.PMHARNESS_PYTHON || venvPython(repoRoot);
  _dbg(`spawning python backend: ${py} cwd=${repoRoot} port=${backendPort}`);
  backend = spawn(py, ["-m", "harness.cli", "gui", "--port", String(backendPort)], {
    cwd: repoRoot,
    env: customEnv,
    stdio: ["ignore", "pipe", "pipe"],
    windowsHide: true,
    // POSIX: own process group so quit can signal the WHOLE tree (backend +
    // workers + codegraph node + wiki). Signalling just the backend pid orphans
    // those children; survivors hold the SQLite lock and ports, and the next
    // fast relaunch dies against them ("stuck until manual restart").
    detached: process.platform !== "win32",
  });

  backend.on("error", (e) => _dbg(`spawn error: ${e.message}`));
  // Recover from an unexpected backend death instead of leaving the window
  // stranded against a dead port (graph/wiki/terminal all fail at once until the
  // user reopens). cleanupBackend() nulls `backend` on intentional teardown, so a
  // non-null ref here means the exit was NOT us -> respawn and tell the renderer.
  backend.on("exit", (code, signal) => {
    const wasOurs = backend;   // non-null => unexpected (not cleanupBackend/quit)
    backend = null;
    if (!wasOurs || quitting) return;
    _dbg(`[backend EXITED unexpectedly] code=${code} signal=${signal} -- respawning`);
    unlinkMarker();
    // Crash-loop guard: if it keeps dying, stop auto-respawning and wait for the
    // next window activate so we don't spin the CPU fighting a hard failure.
    const now = Date.now();
    respawnTimes = respawnTimes.filter((t) => now - t < 60000);
    respawnTimes.push(now);
    if (respawnTimes.length > 5) {
      _dbg("[backend] too many respawns in 60s -- pausing auto-respawn until next activate");
      return;
    }
    startBackend()
      .then(() => {
        _dbg(`[backend] respawned on ${backendPort}`);
        try {
          if (win && win.webContents && !win.webContents.isDestroyed()) {
            // Re-point the renderer at the new port. The main-process IPC bridge
            // (backendRequest + harness:stream) already reads the updated
            // backendPort, but any direct window.__HARNESS_PORT__ consumer would
            // otherwise stay bound to the dead port -- that is the "UI goes dark at
            // finish-time" stranding. Re-inject it and signal panels to re-fetch.
            win.webContents.executeJavaScript(`window.__HARNESS_PORT__=${backendPort};window.__HARNESS_TOKEN__=${JSON.stringify(harnessToken)};`).catch(() => {});
            win.webContents.send("backend:respawned", backendPort);
          }
        } catch { /* window gone */ }
      })
      .catch((e) => _dbg(`[backend] respawn failed: ${e && e.message}`));
  });
  backend.stdout.on("data", (d) => { _dbg(`[out] ${d}`); process.stdout.write(`[backend] ${d}`); });
  backend.stderr.on("data", (d) => { _dbg(`[err] ${d}`); process.stderr.write(`[backend] ${d}`); });
  await waitForBackend(backendPort);
  try { fs.writeFileSync(markerPath(), JSON.stringify({ port: backendPort, pid: backend.pid, at: Date.now() })); } catch {}
}

// ---- transport seam over IPC: proxy to the local backend ----
function authToken() {
  // Main owns the token (handed to the backend via HARNESS_TOKEN and to the
  // renderer at injection time). Return it directly rather than reading the
  // shared file, which a stale second backend could have overwritten.
  return harnessToken || "";
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

ipcMain.on("harness:rendererError", (_e, payload) => {
  const p = payload || {};
  logMain(`[rendererError:${p.scope || "app"}] ${p.message || ""}\n${p.stack || ""}${p.componentStack ? `\ncomponentStack:${p.componentStack}` : ""}`);
});
ipcMain.handle("harness:getJSON", (_e, p) => backendRequest("GET", p));
ipcMain.handle("harness:postJSON", (_e, p, body) => backendRequest("POST", p, body));

// Guards against overlapping restarts (double-click / rapid toggle).
let restarting = false;

// Graceful backend restart: the Hermes-style "apply self-edits" action. Persist
// the live transcript, tear down the current backend (intentional -> the exit
// handler sees `backend === null` and does NOT auto-respawn), spawn a fresh one
// (which, in self-dev mode, imports the just-edited source), then reload the
// renderer so it re-fetches the persisted transcript. The backend flags an
// unanswered user turn via /api/session/state.resume_pending so the UI auto-
// continues -- the conversation survives the swap instead of being dropped.
async function restartBackend() {
  if (restarting) return { ok: false, error: "restart already in progress" };
  restarting = true;
  try {
    // Best-effort: flush the current transcript before we kill the backend, so
    // the fresh process restores exactly where we left off.
    try { await backendRequest("POST", "/api/session/persist", {}); } catch { /* older backend: relies on per-turn saves */ }
    try { cleanupBackend(); } catch { /* already gone */ }
    // Give the OS a beat to release the port/SQLite locks before respawning.
    await new Promise((r) => setTimeout(r, 300));
    await startBackend();
    try {
      if (win && win.webContents && !win.webContents.isDestroyed()) {
        // Re-navigate to the correct renderer source. This also applies a
        // self-dev toggle: on -> Vite HMR (live React), off -> prebuilt dist.
        // did-finish-load re-injects the new backend port/token either way.
        await loadRenderer();
      }
    } catch { /* window gone */ }
    return { ok: true, port: backendPort };
  } catch (e) {
    return { ok: false, error: (e && e.message) || String(e) };
  } finally {
    restarting = false;
  }
}
ipcMain.handle("harness:restart", () => restartBackend());
ipcMain.handle("harness:selfDev:get", () => ({ enabled: selfDevEnabled(), viable: viteDevViable(resolveRepoRoot()) }));
ipcMain.handle("harness:selfDev:set", (_e, enabled) => ({ ok: setSelfDevEnabled(!!enabled), enabled: selfDevEnabled() }));

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
const { buildUpdaterEnv } = require("./update-env.cjs");
registerFsBridge(ipcMain);
registerGitBridge(ipcMain);
// One delivery model (StatusBar's update pill): Marionette always runs from a
// git checkout, so an update is `git pull` + rebuild the source in place, then
// relaunch. There is no signed bundle to swap.
registerUpdateBridge(ipcMain, app, shell, {
  getRepoRoot: resolveRepoRoot,
  // A Finder/Dock launch gets a stripped launchd PATH, so npm/uv are not found
  // and the rebuild spawns with ENOENT ("spawn npm ENOENT") -- the source pulls
  // but the app never rebuilds. Hand the updater the user's real login-shell env
  // (same recovery the backend uses) so its child tools resolve like a terminal.
  getEnv: () => (isDev ? process.env : buildUpdaterEnv({ processEnv: process.env, shellEnv: loginShellEnv() })),
  relaunch: () => {
    try { cleanupBackend(); } catch { /* ignore */ }
    app.relaunch();
    app.exit(0);
  },
});

// Packaged thin shell: bootstrap a source checkout on first launch, streaming
// progress to a small window. Dev/source-tree runs skip this entirely.
let bootstrapWin = null;

function createBootstrapWindow() {
  bootstrapWin = new BrowserWindow({
    width: 520,
    height: 280,
    resizable: false,
    minimizable: false,
    maximizable: false,
    title: "Marionette Setup",
    backgroundColor: "#0f1113",
    webPreferences: { contextIsolation: true, nodeIntegration: false },
  });
  const html = `<!DOCTYPE html><html><head><meta charset="utf-8"><style>
    body{font-family:system-ui,sans-serif;background:#0f1113;color:#e8eaed;margin:0;padding:24px}
    h1{font-size:16px;margin:0 0 8px}#msg{font-size:13px;color:#9aa0a6;margin-bottom:16px;min-height:40px}
    #bar{height:6px;background:#2a2f36;border-radius:3px;overflow:hidden}
    #fill{height:100%;width:0;background:#5b8def;transition:width .2s}
  </style></head><body>
    <h1>Setting up Marionette</h1>
    <div id="msg">Preparing...</div><div id="bar"><div id="fill"></div></div>
  </body></html>`;
  bootstrapWin.loadURL(`data:text/html;charset=utf-8,${encodeURIComponent(html)}`);
  return bootstrapWin;
}

function sendBootstrapProgress(win, msg, pct) {
  try {
    if (win && win.webContents && !win.webContents.isDestroyed()) {
      const js = `document.getElementById('msg').textContent=${JSON.stringify(msg || "")};` +
        `document.getElementById('fill').style.width=${JSON.stringify(String(pct || 0))}+'%';`;
      win.webContents.executeJavaScript(js).catch(() => {});
    }
  } catch { /* window gone */ }
}

async function ensurePackagedCheckout() {
  if (!isPackaged) return resolveRepoRoot();
  const repoRoot = packagedRepoRoot();
  if (isInstallComplete(repoRoot)) {
    process.env.HARNESS_REPO = repoRoot;
    return repoRoot;
  }
  const win = createBootstrapWindow();
  const send = (msg, pct) => sendBootstrapProgress(win, msg, pct);
  try {
    await runBootstrap(repoRoot, send);
    process.env.HARNESS_REPO = repoRoot;
    return repoRoot;
  } finally {
    try { if (bootstrapWin) bootstrapWin.close(); } catch {}
    bootstrapWin = null;
  }
}

function createWindow() {
  win = new BrowserWindow({
    width: 1440, height: 900, backgroundColor: "#0f1113",
    titleBarStyle: "hiddenInset",
    // Windows/Linux render an in-window "File Edit View..." menu bar that
    // stacks a second ugly strip under the title bar (macOS puts the menu in
    // the system bar, so it never shows there). Hide it; Alt still reveals it.
    autoHideMenuBar: true,
    webPreferences: {
      preload: path.join(__dirname, "preload.cjs"),
      contextIsolation: true,
      webviewTag: true,   // enables the real in-app browser
    },
  });
  // expose the backend port to the renderer for any direct needs
  win.webContents.on("did-finish-load", () => {
    win.webContents.executeJavaScript(
      `window.__HARNESS_PORT__=${backendPort};window.__HARNESS_TOKEN__=${JSON.stringify(harnessToken)};`
    ).catch(() => {});
  });
  loadRenderer();

  // Drop the reference when the window is closed so a reopen builds a clean one
  // (and a failed renderer load doesn't leave a half-dead window bound to `win`).
  win.on("closed", () => { win = null; });
  // If the renderer fails to load (white screen / error), reload it once so a
  // transient failure on reopen self-heals instead of stranding the user.
  win.webContents.on("did-fail-load", (_e, errorCode, errorDesc, validatedURL, isMainFrame) => {
    if (isMainFrame && errorCode !== -3) {  // -3 = aborted (navigation), ignore
      _dbg2(`renderer did-fail-load ${errorCode} ${errorDesc} ${validatedURL}`);
      setTimeout(() => {
        try { if (win) loadRenderer(); } catch {}
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
    // Chrome 120 is too old for Google's security checks; use a recent stable.
    const chromeUA =
      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " +
      "(KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36";
    try { ses.setUserAgent(chromeUA); } catch {}
  } catch (e) {
    _dbg2(`browser session config failed: ${e}`);
  }
}

function _dbg2(msg) {
  try { fs.appendFileSync(path.join(os.homedir(), ".pmharness", "electron.log"), `${new Date().toISOString()} ${msg}\n`); } catch {}
}

// Live pop-out windows. Holding strong references here is what makes a pop-out
// PERSIST when the user switches the panel away from the Browser tab: the pane
// (and its <webview>) unmounts, but these are independent top-level windows the
// main process owns, so nothing tears them down. They only close when the user
// closes them, or when the whole app quits.
const popoutWindows = new Set();

// A small always-on-top PIN toggle injected into every pop-out, so the toggle is
// DISCOVERABLE (no menu hunting) and reflects the current pinned state. It calls
// back into main via a tiny synchronous console.* channel we listen for below.
function injectPopoutPinButton(contents, pinned) {
  const js = `(() => {
    try {
      if (window.__mePinInit) { window.__mePinSet && window.__mePinSet(${pinned ? "true" : "false"}); return; }
      window.__mePinInit = true;
      const btn = document.createElement('button');
      btn.id = '__me_pin_btn';
      btn.title = 'Keep this window on top (Cmd/Ctrl+Shift+T)';
      Object.assign(btn.style, {
        position: 'fixed', top: '8px', right: '8px', zIndex: '2147483647',
        width: '34px', height: '24px', borderRadius: '8px', border: 'none',
        cursor: 'pointer', fontSize: '10px', fontWeight: '700', lineHeight: '24px', padding: '0',
        boxShadow: '0 2px 8px rgba(0,0,0,0.4)', userSelect: 'none',
      });
      window.__mePinSet = (on) => {
        btn.textContent = on ? 'PIN' : 'pin';
        btn.style.background = on ? '#2563eb' : 'rgba(30,30,30,0.85)';
        btn.style.color = '#fff';
        btn.style.opacity = on ? '1' : '0.75';
      };
      window.__mePinSet(${pinned ? "true" : "false"});
      btn.addEventListener('click', (e) => {
        e.preventDefault(); e.stopPropagation();
        console.log('__ME_TOGGLE_PIN__');
      });
      const mount = () => { if (document.body) document.body.appendChild(btn); };
      if (document.body) mount(); else document.addEventListener('DOMContentLoaded', mount);
    } catch (_) {}
  })();`;
  try { contents.executeJavaScript(js, true).catch(() => {}); } catch {}
}

// Wire always-on-top toggling (keyboard + injected pin) and persistence onto a
// pop-out window's webContents. Shared by webview-spawned popups and the
// explicit "Open externally" IPC path so both behave identically.
function wirePopoutWindow(win) {
  if (!win || win.isDestroyed()) return;
  popoutWindows.add(win);
  win.on("closed", () => popoutWindows.delete(win));

  const contents = win.webContents;
  const reflectPin = () => injectPopoutPinButton(contents, win.isAlwaysOnTop());
  const togglePin = () => {
    try {
      const next = !win.isAlwaysOnTop();
      win.setAlwaysOnTop(next, "floating");
      reflectPin();
    } catch {}
  };

  // Keyboard toggle: Cmd/Ctrl+Shift+T.
  contents.on("before-input-event", (evt, input) => {
    try {
      const mod = process.platform === "darwin" ? input.meta : input.control;
      if (mod && input.shift && String(input.key).toLowerCase() === "t") {
        togglePin();
        evt.preventDefault();
      }
    } catch {}
  });
  // Injected pin button posts this sentinel through console-message.
  contents.on("console-message", (_evt, _level, message) => {
    if (message === "__ME_TOGGLE_PIN__") togglePin();
  });
  // (Re)inject the pin after every load so SPA navigations keep the toggle.
  contents.on("did-finish-load", reflectPin);
  reflectPin();
}

// When the in-app browser opens a popup (window.open from an OAuth/login page,
// OR a Cmd/Ctrl+click on a link inside the webview), give it a real independent
// BrowserWindow bound to the SAME persistent partition so the login completes
// and its cookie lands in the shared session -- and so the window PERSISTS when
// the Browser panel is swapped away.
// Capture-phase click catcher injected into every in-panel webview: a plain
// <a href> Cmd/Ctrl+click does NOT reliably reach setWindowOpenHandler in an
// Electron webview, so we intercept modified clicks on links ourselves and post
// the resolved URL back through the console-message sentinel channel. This makes
// "Cmd/Ctrl+click a link -> pop it out (always-on-top, persistent)" work on any
// site, matching the pop-out BUTTON behavior.
const POPOUT_CLICK_SENTINEL = "__ME_POPOUT__:";
function injectPopoutClickCatcher(contents) {
  const js = `(() => {
    try {
      if (window.__mePopoutClickInit) return;
      window.__mePopoutClickInit = true;
      document.addEventListener('click', (e) => {
        try {
          const mod = e.metaKey || e.ctrlKey;
          if (!mod || e.button !== 0) return;
          let a = e.target;
          while (a && a.tagName !== 'A') a = a.parentElement;
          if (!a || !a.href) return;
          e.preventDefault(); e.stopPropagation();
          console.log(${JSON.stringify(POPOUT_CLICK_SENTINEL)} + a.href);
        } catch (_) {}
      }, true);
    } catch (_) {}
  })();`;
  try { contents.executeJavaScript(js, true).catch(() => {}); } catch {}
}

app.on("web-contents-created", (_e, contents) => {
  if (contents.getType() === "webview") {
    contents.setWindowOpenHandler(() => {
      return {
        action: "allow",
        overrideBrowserWindowOptions: {
          webPreferences: { partition: "persist:browser", contextIsolation: true },
          width: 600,
          height: 750,
          // Default pinned: pop a video/meeting out, then go back to the
          // editor/terminal pane without it vanishing behind the app.
          alwaysOnTop: true,
        },
      };
    });
    // Attach persistence + pin toggle to the freshly created pop-out window.
    contents.on("did-create-window", (childWindow) => {
      try { childWindow.setAlwaysOnTop(true, "floating"); } catch {}
      wirePopoutWindow(childWindow);
    });
    // Hide automation signals on every navigation so Google/Gmail login
    // doesn't flag the in-app browser as "unsafe". Electron's <webview>
    // leaks navigator.webdriver=true, zero plugins, and absence of
    // navigator.languages -- all of which trigger bot-detection.
    const hideAutomation = () => {
      try {
        contents.executeJavaScript(`(() => {
          try {
            if (window.__pmAutomationHidden) return;
            window.__pmAutomationHidden = true;
            Object.defineProperty(navigator, 'webdriver', { get: () => false });
            const opl = navigator.plugins;
            Object.defineProperty(navigator, 'plugins', {
              get: () => opl.length > 0 ? opl : [
                { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer', description: 'Portable Document Format' }
              ]
            });
            if (!navigator.languages || navigator.languages.length === 0) {
              Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
            }
            const omt = navigator.mimeTypes;
            Object.defineProperty(navigator, 'mimeTypes', {
              get: () => omt.length > 0 ? omt : [
                { type: 'application/pdf', suffixes: 'pdf', description: 'Portable Document Format' }
              ]
            });
          } catch (_) {}
        })();`).catch(() => {});
      } catch (_) {}
    };
    contents.on("did-finish-load", () => { hideAutomation(); injectPopoutClickCatcher(contents); });
    contents.on("console-message", (_evt, _level, message) => {
      if (typeof message === "string" && message.startsWith(POPOUT_CLICK_SENTINEL)) {
        const url = message.slice(POPOUT_CLICK_SENTINEL.length);
        try { openPopoutWindow(url); } catch (err) { logMain(`popout click failed: ${err && err.message ? err.message : err}`); }
      }
    });
  }
});

// Explicit pop-out from the renderer ("Open externally" button / Cmd+click):
// create a standalone, always-on-top, persistent browser window.
// Shared standalone-popout factory: an always-on-top, persistent, main-process
// BrowserWindow on the shared browser partition. Used by BOTH the explicit
// "Pop out" button/IPC and the injected Cmd/Ctrl+click catcher so every pop-out
// behaves identically (floats on top, survives switching off the Browser tab).
function openPopoutWindow(url) {
  const target = typeof url === "string" && url.trim() ? url.trim() : "about:blank";
  const win = new BrowserWindow({
    width: 900,
    height: 700,
    alwaysOnTop: true,
    title: "Browser",
    backgroundColor: "#0f1113",
    webPreferences: {
      partition: "persist:browser",
      contextIsolation: true,
      nodeIntegration: false,
    },
  });
  try { win.setAlwaysOnTop(true, "floating"); } catch {}
  wirePopoutWindow(win);
  win.loadURL(target);
  return win;
}

ipcMain.handle("browser:popout", (_e, url) => {
  try {
    openPopoutWindow(url);
    return { ok: true };
  } catch (e) {
    logMain(`browser:popout failed: ${e && e.message ? e.message : e}`);
    return { ok: false, error: String(e && e.message ? e.message : e) };
  }
});

app.whenReady().then(async () => {
  if (!gotSingleInstanceLock) return; // a prior instance owns the backend
  configureBrowserSession();
  // A Finder/Dock launch inherits a minimal launchd PATH that omits Homebrew and
  // Node version managers, so the FIRST-RUN bootstrap (git/node/uv discovery) can
  // wrongly fail with "Node too old / not found" even when the user has a modern
  // Node in their terminal (real report: Homebrew Node v26 rejected). Merge the
  // user's real login-shell PATH into this process BEFORE bootstrap so tool
  // discovery matches their terminal. bootstrap.cjs also hydrates PATH as a
  // second layer of defense.
  if (isPackaged && process.platform !== "win32") {
    try {
      const shellPath = loginShellEnv().PATH;
      if (shellPath) {
        const have = new Set(String(process.env.PATH || "").split(path.delimiter));
        const extra = String(shellPath).split(path.delimiter).filter((p) => p && !have.has(p));
        if (extra.length) process.env.PATH = extra.join(path.delimiter) + path.delimiter + (process.env.PATH || "");
      }
    } catch { /* fall back to bootstrap's hydratePath() */ }
  }
  if (isPackaged) {
    try { await ensurePackagedCheckout(); } catch (e) {
      console.error("bootstrap failed:", e);
      dialog.showErrorBox("Marionette setup failed", String(e && e.message ? e.message : e));
      app.quit();
      return;
    }
  }
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

function killBackendTree(b) {
  // Kill the backend AND its children (workers, codegraph node, wiki backend).
  // Killing only the backend pid leaves orphans holding the SQLite lock and
  // ports; a fast relaunch then spawns a backend that dies against them and the
  // app sits "stuck" until a manual restart from Settings.
  if (process.platform === "win32") {
    // taskkill /T walks the child tree; TerminateProcess via b.kill() does not.
    try {
      require("node:child_process").spawnSync(
        "taskkill", ["/pid", String(b.pid), "/T", "/F"],
        { windowsHide: true, timeout: 5000 },
      );
    } catch {}
    try { b.kill(); } catch {}
    return;
  }
  // POSIX: the backend was spawned detached (own process group), so a negative
  // pid signals the whole group.
  try { process.kill(-b.pid, "SIGTERM"); } catch { try { b.kill("SIGTERM"); } catch {} }
  try {
    setTimeout(() => {
      try { process.kill(-b.pid, "SIGKILL"); } catch { try { if (!b.killed) b.kill("SIGKILL"); } catch {} }
    }, 800);
  } catch {}
}

function cleanupBackend() {
  // Tear the backend down on a REAL quit so the next launch always starts a fresh
  // backend running the latest code. Two things matter for the "Cmd+Q then reopen
  // picks up my changes" workflow:
  //   1. Remove the marker FIRST -- startBackend adopts any healthy backend it
  //      finds on the marker port, so a lingering survivor would be reused (old
  //      code). No marker => reopen can only spawn fresh.
  //   2. Kill the whole process tree (see killBackendTree), so a backend that is
  //      slow to exit (mid tool call / draining) can never survive into the next
  //      session and strand the relaunch.
  unlinkMarker();
  if (backend) {
    const b = backend;
    backend = null;
    killBackendTree(b);
  }
}
app.on("window-all-closed", () => {
  // On macOS the app stays alive when all windows close (standard behavior), so
  // we MUST keep the backend running -- otherwise reopening a window (Cmd/Ctrl+W
  // then reopen, or Dock click) loads a renderer against a dead backend and every
  // API call errors. The backend is torn down only on a real quit (before-quit).
  if (process.platform !== "darwin") {
    cleanupBackend();
    cleanupVite();
    app.quit();
  }
});
let quitFinalized = false;
app.on("before-quit", (e) => {
  quitting = true;
  if (quitFinalized) return;
  // Hold the quit open just long enough for the SIGTERM->SIGKILL grace timer to
  // run. Without this the event loop tears down at ~0ms, the escalation never
  // fires, and a slow-to-exit backend survives into the next launch -- the
  // "closed fast, now it's stuck until manual restart" failure.
  e.preventDefault();
  cleanupBackend();
  cleanupVite();
  setTimeout(() => { quitFinalized = true; app.quit(); }, 1000);
});
