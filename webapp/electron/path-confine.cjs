// Path confinement for Electron IPC bridges (fs / git). Mirrors harness.paths
// path_within: resolve both sides, fold Windows case, fail closed.
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

function pmharnessHome() {
  return path.join(os.homedir(), ".pmharness");
}

function pmharnessStateDir() {
  return path.join(pmharnessHome(), "state");
}

/** Read a state file from state/ first, then legacy ~/.pmharness/. */
function readPmHarnessStateFile(name) {
  for (const dir of [pmharnessStateDir(), pmharnessHome()]) {
    try {
      return fs.readFileSync(path.join(dir, name), "utf8");
    } catch {
      /* try next */
    }
  }
  return null;
}

/**
 * Resolve a path for containment checks. Prefer realpath when the path
 * exists; otherwise path.resolve so missing targets still reject traversal.
 */
function resolveForContainment(p) {
  const abs = path.resolve(String(p || ""));
  try {
    return fs.realpathSync(abs);
  } catch {
    return abs;
  }
}

/**
 * True when ``candidate`` is inside ``parent`` (or equal when allow_equal).
 * Never throws — unresolvable / cross-volume comparisons return false.
 */
function pathWithin(candidate, parent, opts = {}) {
  const allowEqual = opts.allow_equal !== false;
  try {
    let realPath = resolveForContainment(candidate);
    let realParent = resolveForContainment(parent);
    if (process.platform === "win32") {
      realPath = realPath.toLowerCase();
      realParent = realParent.toLowerCase();
    }
    const rel = path.relative(realParent, realPath);
    if (rel === "") return Boolean(allowEqual);
    if (rel.startsWith("..") || path.isAbsolute(rel)) return false;
    return true;
  } catch {
    return false;
  }
}

/**
 * Active workspace + recents from workspace.json (same sources the backend
 * restores). Empty when nothing is configured — callers must fail closed.
 */
function loadWorkspaceAllowedRoots(readStateFile = readPmHarnessStateFile) {
  const raw = readStateFile("workspace.json");
  if (!raw) return [];
  let data;
  try {
    data = JSON.parse(raw);
  } catch {
    return [];
  }
  const roots = [];
  const push = (p) => {
    const s = String(p || "").trim();
    if (!s) return;
    if (roots.some((r) => pathWithin(s, r) && pathWithin(r, s))) return;
    roots.push(path.resolve(s));
  };
  push(data.repo);
  for (const r of data.recents || []) push(r);
  return roots;
}

/**
 * Reject paths outside every allowed root. Returns null when allowed, or an
 * error string when denied / missing roots.
 */
function denyOutsideAllowedRoots(targetPath, allowedRoots) {
  if (!targetPath || typeof targetPath !== "string") {
    return "missing path";
  }
  const roots = Array.isArray(allowedRoots) ? allowedRoots.filter(Boolean) : [];
  if (roots.length === 0) {
    return "path not allowed (no workspace root)";
  }
  const abs = path.resolve(targetPath);
  if (roots.some((root) => pathWithin(abs, root, { allow_equal: true }))) {
    return null;
  }
  return "path not allowed";
}

module.exports = {
  pathWithin,
  resolveForContainment,
  loadWorkspaceAllowedRoots,
  denyOutsideAllowedRoots,
  readPmHarnessStateFile,
  pmharnessHome,
  pmharnessStateDir,
};
