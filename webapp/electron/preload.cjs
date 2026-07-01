// Preload: exposes window.harnessIPC implementing the renderer's transport seam
// (lib/transport.ts checks for window.harnessIPC and routes through it). Plus
// native fs/git bridges for the file-tree and source-control panels.
const { contextBridge, ipcRenderer } = require("electron");

let streamSeq = 0;

contextBridge.exposeInMainWorld("harnessIPC", {
  getJSON: (path) => ipcRenderer.invoke("harness:getJSON", path),
  postJSON: (path, body) => ipcRenderer.invoke("harness:postJSON", path, body),
  pickFolder: () => ipcRenderer.invoke("harness:pickFolder"),
  uploadFile: (payload) => ipcRenderer.invoke("harness:uploadFile", payload),

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
    apply: () => ipcRenderer.invoke("updates:apply"),
    openRepo: (sub) => ipcRenderer.invoke("updates:openRepo", sub),
    onProgress: (cb) => {
      const handler = (_e, payload) => cb(payload);
      ipcRenderer.on("updates:progress", handler);
      return () => ipcRenderer.removeListener("updates:progress", handler);
    },
  },
  isDesktop: true,
});
