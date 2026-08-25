// Preload: exposes window.harnessIPC implementing the renderer's transport seam
// (lib/transport.ts checks for window.harnessIPC and routes through it). Plus
// native fs/git bridges for the file-tree and source-control panels.
const { contextBridge, ipcRenderer, webUtils } = require("electron");

let streamSeq = 0;

contextBridge.exposeInMainWorld("harnessIPC", {
  getJSON: (path) => ipcRenderer.invoke("harness:getJSON", path),
  postJSON: (path, body) => ipcRenderer.invoke("harness:postJSON", path, body),
  pickFolder: () => ipcRenderer.invoke("harness:pickFolder"),
  popoutBrowser: (url) => ipcRenderer.invoke("browser:popout", url),
  // Open a URL in the OS default browser (escape hatch when in-app Google OAuth rejects).
  openExternal: (url) => ipcRenderer.invoke("browser:openExternal", url),
  closeWindow: () => ipcRenderer.invoke("window:close"),
  onCloseTab: (cb) => {
    const handler = () => { try { cb(); } catch (_) {} };
    ipcRenderer.on("app:closeTab", handler);
    return () => ipcRenderer.removeListener("app:closeTab", handler);
  },
  onOpenInApp: (cb) => {
    const handler = (_e, url) => { try { cb(url); } catch (_) {} };
    ipcRenderer.on("browser:openInApp", handler);
    return () => ipcRenderer.removeListener("browser:openInApp", handler);
  },
  uploadFile: (payload) => ipcRenderer.invoke("harness:uploadFile", payload),
  // Electron no longer sets File.path; this is the supported drop-path API.
  pathForFile: (file) => {
    try {
      if (webUtils && typeof webUtils.getPathForFile === "function") {
        return webUtils.getPathForFile(file) || "";
      }
    } catch (_) {}
    return "";
  },
  // User-initiated drop/open: main owns filesystem access because Electron's
  // sandboxed preload cannot import Node's fs module.
  isDirectory: (absPath) => ipcRenderer.invoke("fs:isDirectory", absPath),
  // Fire-and-forget: persist a caught renderer error to the Electron main log so
  // a UI crash is diagnosable from ~/.pmharness/electron.log without devtools.
  logError: (payload) => { try { ipcRenderer.send("harness:rendererError", payload); } catch (_) {} },
  secrets: {
    save: (payload) => ipcRenderer.invoke("secrets:save", payload),
    presence: (payload) => ipcRenderer.invoke("secrets:presence", payload),
  },

  // stream(path, onEvent, onDone, onError) -> cancel()
  stream: (path, onEvent, onDone, onError) => {
    const id = `stream-${++streamSeq}`;
    const onEv = (_e, ev) => onEvent(ev);
    const onDoneCb = () => { cleanup(); onDone && onDone(); };
    const onErrCb = (_e, err) => { cleanup(); onError && onError(err); };
    const cleanup = () => {
      ipcRenderer.removeListener(`${id}:event`, onEv);
      ipcRenderer.removeListener(`${id}:done`, onDoneCb);
      ipcRenderer.removeListener(`${id}:error`, onErrCb);
    };
    ipcRenderer.on(`${id}:event`, onEv);
    ipcRenderer.on(`${id}:done`, onDoneCb);
    ipcRenderer.on(`${id}:error`, onErrCb);
    ipcRenderer.send("harness:stream", id, path);
    return () => { ipcRenderer.send(`${id}:cancel`); cleanup(); };
  },

  // native bridges
  fs: {
    readDir: (dir) => ipcRenderer.invoke("fs:readDir", dir),
    readFile: (file) => ipcRenderer.invoke("fs:readFile", file),
    revealInFolder: (absPath) => ipcRenderer.invoke("fs:revealInFolder", absPath),
  },
  git: {
    status: (repo) => ipcRenderer.invoke("git:status", repo),
    diff: (repo, file) => ipcRenderer.invoke("git:diff", repo, file),
    branches: (repo) => ipcRenderer.invoke("git:branches", repo),
    stageFile: (repo, file) => ipcRenderer.invoke("git:stageFile", repo, file),
    unstageFile: (repo, file) => ipcRenderer.invoke("git:unstageFile", repo, file),
    stageAll: (repo) => ipcRenderer.invoke("git:stageAll", repo),
    unstageAll: (repo) => ipcRenderer.invoke("git:unstageAll", repo),
    commit: (repo, message) => ipcRenderer.invoke("git:commit", repo, message),
    diffStaged: (repo, file) => ipcRenderer.invoke("git:diffStaged", repo, file),
    applyHunk: (repo, patchText, reverse) => ipcRenderer.invoke("git:applyHunk", repo, patchText, reverse),
  },
  // Self-update: how far behind the tracked branch we are, apply (pull+rebuild),
  // and a progress subscription for the apply. openRepo opens the repo/commits.
  updates: {
    check: () => ipcRenderer.invoke("updates:check"),
    apply: (opts) => ipcRenderer.invoke("updates:apply", opts),
    openRepo: (sub) => ipcRenderer.invoke("updates:openRepo", sub),
    openReleases: () => ipcRenderer.invoke("updates:openReleases"),
    onProgress: (cb) => {
      const handler = (_e, payload) => cb(payload);
      ipcRenderer.on("updates:progress", handler);
      return () => ipcRenderer.removeListener("updates:progress", handler);
    },
    // Push notification from the main-process update watcher: fires with the
    // check result whenever a background fetch finds the checkout behind.
    onAvailable: (cb) => {
      const handler = (_e, payload) => cb(payload);
      ipcRenderer.on("updates:available", handler);
      return () => ipcRenderer.removeListener("updates:available", handler);
    },
  },
  // Live self-editing (Hermes-style): toggle running the backend from the
  // editable source checkout, and restart it to apply self-edits without a
  // full app relaunch. restart() reloads the renderer once the fresh backend
  // is up; the conversation resumes from the persisted transcript.
  selfDev: {
    get: () => ipcRenderer.invoke("harness:selfDev:get"),
    set: (enabled) => ipcRenderer.invoke("harness:selfDev:set", enabled),
  },
  translucency: {
    get: () => ipcRenderer.invoke("translucency:get"),
    set: (state) => ipcRenderer.invoke("translucency:set", state),
    capabilities: () => ipcRenderer.invoke("translucency:capabilities"),
  },
  restart: () => ipcRenderer.invoke("harness:restart"),
  // Fired when main respawns the Python backend on a new port. Panels that
  // painted a transient ECONNREFUSED can re-fetch without a full window reload.
  onBackendRespawned: (cb) => {
    const handler = (_e, port) => { try { cb(port); } catch (_) {} };
    ipcRenderer.on("backend:respawned", handler);
    return () => ipcRenderer.removeListener("backend:respawned", handler);
  },
  // Fired when portablellm.wiki hands back a personal LLM URL via
  // marionette://wiki-connect (Electron protocol / webview intercept).
  onWikiConnected: (cb) => {
    const handler = (_e, payload) => { try { cb(payload); } catch (_) {} };
    ipcRenderer.on("wiki:connected", handler);
    return () => ipcRenderer.removeListener("wiki:connected", handler);
  },
  isDesktop: true,
});
