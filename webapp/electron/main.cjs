// Electron main process for pm-harness.
// Responsibilities:
//  1. Spawn the Python harness backend (harness.cli gui) on a loopback port.
//  2. Create the BrowserWindow loading the Vite build (or dev server).
//  3. Register IPC handlers that back the renderer's transport seam
//     (window.harnessIPC.getJSON/postJSON/stream) + native fs/git bridges.
// The renderer is the SAME React app as the web build; only the transport
// implementation differs (IPC here vs fetch/SSE on the web).

const { app, BrowserWindow, ipcMain } = require("electron");
const { spawn } = require("node:child_process");
const http = require("node:http");
const path = require("node:path");
const net = require("node:net");
const fs = require("node:fs");
const os = require("node:os");

const isDev = !!process.env.PMHARNESS_DEV_SERVER;
let backend = null;
let backendPort = 8799;
let win = null;

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

  // 2. Spawn a fresh backend on a free port and record the marker.
  backendPort = await freePort();
  const repoRoot = path.resolve(__dirname, "..", "..");
  const py = process.env.PMHARNESS_PYTHON || path.join(repoRoot, ".venv", "bin", "python");
  backend = spawn(py, ["-m", "harness.cli", "gui", "--port", String(backendPort)], {
    cwd: repoRoot,
    env: { ...process.env, HARNESS_REPO: process.env.HARNESS_REPO || repoRoot },
    stdio: ["ignore", "pipe", "pipe"],
  });
  backend.stdout.on("data", (d) => process.stdout.write(`[backend] ${d}`));
  backend.stderr.on("data", (d) => process.stderr.write(`[backend] ${d}`));
  await waitForBackend(backendPort);
  try { fs.writeFileSync(markerPath(), JSON.stringify({ port: backendPort, pid: backend.pid, at: Date.now() })); } catch {}
}

// ---- transport seam over IPC: proxy to the local backend ----
function backendRequest(method, apiPath, body) {
  return new Promise((resolve, reject) => {
    const data = body ? JSON.stringify(body) : null;
    const req = http.request({
      host: "127.0.0.1", port: backendPort, path: apiPath, method,
      headers: { "Content-Type": "application/json", ...(data ? { "Content-Length": Buffer.byteLength(data) } : {}) },
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

// SSE stream: bridge backend EventSource-style stream to renderer via events.
ipcMain.on("harness:stream", (event, channelId, apiPath) => {
  const req = http.get({ host: "127.0.0.1", port: backendPort, path: apiPath }, (res) => {
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
          if (ev.kind === "done") { event.sender.send(`${channelId}:done`); res.destroy(); return; }
          event.sender.send(`${channelId}:event`, ev);
        } catch {}
      }
    });
    res.on("end", () => event.sender.send(`${channelId}:done`));
    res.on("error", (e) => event.sender.send(`${channelId}:error`, String(e)));
  });
  req.on("error", (e) => event.sender.send(`${channelId}:error`, String(e)));
  ipcMain.once(`${channelId}:cancel`, () => req.destroy());
});

// ---- native bridges (file tree + git) ----
const { registerFsBridge } = require("./fs-bridge.cjs");
const { registerGitBridge } = require("./git-bridge.cjs");
registerFsBridge(ipcMain);
registerGitBridge(ipcMain);

function createWindow() {
  win = new BrowserWindow({
    width: 1440, height: 900, backgroundColor: "#0d0d0f",
    titleBarStyle: "hiddenInset",
    webPreferences: {
      preload: path.join(__dirname, "preload.cjs"),
      contextIsolation: true,
      webviewTag: true,   // enables the real in-app browser
    },
  });
  // expose the backend port to the renderer for any direct needs
  win.webContents.on("did-finish-load", () => {
    win.webContents.executeJavaScript(`window.__HARNESS_PORT__=${backendPort};`).catch(() => {});
  });
  if (isDev) win.loadURL(process.env.PMHARNESS_DEV_SERVER);
  else win.loadFile(path.join(__dirname, "..", "dist", "index.html"));
}

app.whenReady().then(async () => {
  try { await startBackend(); } catch (e) { console.error("backend start failed:", e); }
  createWindow();
  app.on("activate", () => { if (BrowserWindow.getAllWindows().length === 0) createWindow(); });
});

function cleanupBackend() {
  if (backend) {
    try { fs.unlinkSync(markerPath()); } catch {}
    backend.kill();
    backend = null;
  }
}
app.on("window-all-closed", () => {
  cleanupBackend();
  if (process.platform !== "darwin") app.quit();
});
app.on("before-quit", cleanupBackend);
