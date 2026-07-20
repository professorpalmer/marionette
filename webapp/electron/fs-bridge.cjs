// Native filesystem bridge for the file-explorer pane. Adapted from the Hermes
// Agent desktop fs-read-dir.cjs pattern (MIT, Nous Research): read a directory
// into a tree node list, ignoring heavy/noise dirs. Reveal uses Electron shell.
const fs = require("node:fs");
const path = require("node:path");
const { shell } = require("electron");
const {
  denyOutsideAllowedRoots,
  loadWorkspaceAllowedRoots,
} = require("./path-confine.cjs");

const IGNORE = new Set([".git", "node_modules", ".venv", "venv", "__pycache__",
  ".codegraph", "dist", ".vite", ".DS_Store", ".pytest_cache", ".ruff_cache"]);

/**
 * @param {import("electron").IpcMain} ipcMain
 * @param {{ getAllowedRoots?: () => string[] }} [opts]
 */
function registerFsBridge(ipcMain, opts = {}) {
  const getAllowedRoots =
    typeof opts.getAllowedRoots === "function"
      ? opts.getAllowedRoots
      : () => loadWorkspaceAllowedRoots();

  const guard = (targetPath) =>
    denyOutsideAllowedRoots(targetPath, getAllowedRoots());

  ipcMain.handle("fs:readDir", (_e, dir) => {
    try {
      const denied = guard(dir);
      if (denied) return { ok: false, error: denied };
      const entries = fs.readdirSync(dir, { withFileTypes: true });
      const nodes = entries
        .filter((d) => !IGNORE.has(d.name))
        .map((d) => ({
          name: d.name,
          path: path.join(dir, d.name),
          dir: d.isDirectory(),
        }))
        .sort((a, b) => (a.dir === b.dir ? a.name.localeCompare(b.name) : a.dir ? -1 : 1));
      return { ok: true, nodes };
    } catch (e) {
      return { ok: false, error: String(e) };
    }
  });

  ipcMain.handle("fs:readFile", (_e, file) => {
    try {
      const denied = guard(file);
      if (denied) return { ok: false, error: denied };
      const stat = fs.statSync(file);
      if (stat.size > 2_000_000) return { ok: false, error: "file too large" };
      return { ok: true, content: fs.readFileSync(file, "utf8") };
    } catch (e) {
      return { ok: false, error: String(e) };
    }
  });

  // Reveal a file or folder in the OS file manager (Finder / Explorer).
  // Caller must pass an absolute path; relative workspace paths are resolved
  // in the renderer before invoke.
  ipcMain.handle("fs:revealInFolder", (_e, absPath) => {
    try {
      if (!absPath || typeof absPath !== "string") {
        return { ok: false, error: "missing path" };
      }
      const denied = guard(absPath);
      if (denied) return { ok: false, error: denied };
      shell.showItemInFolder(absPath);
      return { ok: true };
    } catch (e) {
      return { ok: false, error: String(e) };
    }
  });
}

module.exports = { registerFsBridge };
