// Bootstrap a Marionette source checkout for the packaged thin Electron shell.
// When the app is installed from a release build (DMG/NSIS/AppImage), it does NOT
// bundle Python or a frozen backend. On first launch it clones the repo into
// ~/.marionette/marionette, provisions uv + node + git as needed, builds the venv
// and renderer, then hands off to main.cjs for normal source-run operation.
//
// Node stdlib + child_process only. Progress is streamed via onProgress(message, pct).

"use strict";

const { spawn, spawnSync } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const http = require("node:http");
const https = require("node:https");

// Keep in sync with scripts/versions.env
const VERSIONS = {
  NODE: "22.14.0",
  NODE_MIN_MAJOR: 20,
  MINGIT: "2.55.0",
  SHA: {
    NODE_WIN_X64: "55b639295920b219bb2acbcfa00f90393a2789095b7323f79475c9f34795f217",
    NODE_WIN_ARM64: "2d71f5f9b2fffa33baa108c07d74b0d24e0c3dd8f441d567772ae0e3dd4b1a22",
    MINGIT_WIN_X64: "31497e7968196332263459ee319d2524e3ebc5786ab895e2abad34ffdd4f4ebf",
    MINGIT_WIN_ARM64: "377e283290e2de455cdd5cdbd99653bd911db752a8986d1ad914a5ac2fbd1192",
  },
};

const DEFAULT_REPO = "https://github.com/professorpalmer/marionette.git";
const DEFAULT_BRANCH = "main";

function venvPython(dir) {
  return process.platform === "win32"
    ? path.join(dir, ".venv", "Scripts", "python.exe")
    : path.join(dir, ".venv", "bin", "python");
}

function isInstallComplete(dir) {
  try {
    return (
      fs.existsSync(path.join(dir, ".git")) &&
      fs.existsSync(venvPython(dir)) &&
      fs.existsSync(path.join(dir, "webapp", "dist", "index.html"))
    );
  } catch {
    return false;
  }
}

function run(cmd, args, opts = {}) {
  const res = spawnSync(cmd, args, {
    encoding: "utf8",
    stdio: opts.inherit ? "inherit" : "pipe",
    env: opts.env || process.env,
    cwd: opts.cwd,
    shell: opts.shell || false,
    windowsHide: true,
  });
  if (res.status !== 0) {
    const detail = (res.stderr || res.stdout || "").trim();
    throw new Error(`${cmd} ${args.join(" ")} failed (code ${res.status})${detail ? ": " + detail.slice(0, 500) : ""}`);
  }
  return res;
}

// On Windows, npm is a .cmd batch shim, not an executable. Node (post
// CVE-2024-27980) refuses to spawn .cmd files without shell:true, and the
// failed spawn surfaces as status:null -- the "npm ci failed (code null)"
// first-run error. Route npm through a shell on win32 only.
function runNpm(args, opts = {}) {
  if (process.platform === "win32") {
    return run("npm.cmd", args, { ...opts, shell: true });
  }
  return run("npm", args, opts);
}

function commandExists(name) {
  const check = process.platform === "win32" ? "where" : "which";
  const res = spawnSync(check, [name], { encoding: "utf8", windowsHide: true });
  return res.status === 0;
}

function nodeMajor() {
  if (!commandExists("node")) return 0;
  const res = spawnSync("node", ["-v"], { encoding: "utf8", windowsHide: true });
  if (res.status !== 0) return 0;
  return parseInt(String(res.stdout).replace(/^v/, "").split(".")[0], 10) || 0;
}

function downloadFile(url, dest) {
  return new Promise((resolve, reject) => {
    const proto = url.startsWith("https") ? https : http;
    const file = fs.createWriteStream(dest);
    proto.get(url, (res) => {
      if (res.statusCode >= 300 && res.statusCode < 400 && res.headers.location) {
        file.close();
        fs.unlinkSync(dest);
        return downloadFile(res.headers.location, dest).then(resolve, reject);
      }
      if (res.statusCode !== 200) {
        file.close();
        fs.unlinkSync(dest);
        return reject(new Error(`download failed: ${url} (${res.statusCode})`));
      }
      res.pipe(file);
      file.on("finish", () => file.close(() => resolve(dest)));
    }).on("error", (e) => {
      try { fs.unlinkSync(dest); } catch {}
      reject(e);
    });
  });
}

function verifySha256(file, expected) {
  if (!expected) return;
  const crypto = require("node:crypto");
  const hash = crypto.createHash("sha256").update(fs.readFileSync(file)).digest("hex");
  if (hash !== expected.toLowerCase()) {
    throw new Error(`checksum mismatch for ${path.basename(file)}`);
  }
}

function winArch() {
  if (process.arch === "arm64") return "arm64";
  return "x64";
}

function toolRoot() {
  const base = process.platform === "win32"
    ? path.join(process.env.LOCALAPPDATA || path.join(os.homedir(), "AppData", "Local"), "marionette", "tools")
    : path.join(os.homedir(), ".marionette", "tools");
  fs.mkdirSync(base, { recursive: true });
  return base;
}

function addToPath(dir) {
  if (!dir || process.env.PATH.split(path.delimiter).includes(dir)) return;
  process.env.PATH = dir + path.delimiter + process.env.PATH;
}

// A macOS/Linux GUI app launched from Finder/Dock inherits a MINIMAL PATH
// (/usr/bin:/bin:/usr/sbin:/sbin) that excludes Homebrew and every Node version
// manager -- so `which node` fails even when the user has a modern Node, and the
// bootstrap wrongly reports "Node too old / not found" (real report: Node v26 via
// Homebrew rejected). Prepend the standard install locations so tool discovery
// matches what the user sees in their terminal. Best-effort and idempotent;
// only real, existing dirs are added.
function hydratePath() {
  if (process.platform === "win32") return;
  const home = os.homedir();
  const candidates = [
    "/opt/homebrew/bin",      // Apple Silicon Homebrew
    "/usr/local/bin",         // Intel Homebrew + common installs
    "/opt/local/bin",         // MacPorts
    path.join(home, ".local", "bin"),
    path.join(home, ".volta", "bin"),
    path.join(home, ".fnm"),
    path.join(home, "n", "bin"),
  ];
  // Version managers keep the active Node under a versioned dir; add the newest.
  for (const vm of [path.join(home, ".nvm", "versions", "node"),
                    path.join(home, ".local", "share", "fnm", "node-versions")]) {
    try {
      const versions = fs.readdirSync(vm)
        .filter((v) => /^v?\d/.test(v))
        .sort()
        .reverse();
      for (const v of versions) {
        const bin = path.join(vm, v, process.platform === "win32" ? "" : "bin");
        if (fs.existsSync(path.join(bin, "node"))) { candidates.unshift(bin); break; }
        // fnm nests under <version>/installation/bin on some setups
        const alt = path.join(vm, v, "installation", "bin");
        if (fs.existsSync(path.join(alt, "node"))) { candidates.unshift(alt); break; }
      }
    } catch { /* no such manager */ }
  }
  for (const dir of candidates) {
    try { if (fs.existsSync(dir)) addToPath(dir); } catch { /* ignore */ }
  }
}

async function ensurePortableNode(onProgress) {
  if (nodeMajor() >= VERSIONS.NODE_MIN_MAJOR) return;
  if (process.platform !== "win32") {
    throw new Error(`Node >= v${VERSIONS.NODE_MIN_MAJOR} is required. Install Node from https://nodejs.org and relaunch.`);
  }
  const arch = winArch();
  const zipName = `node-v${VERSIONS.NODE}-win-${arch}.zip`;
  const url = `https://nodejs.org/dist/v${VERSIONS.NODE}/${zipName}`;
  const expected = arch === "arm64" ? VERSIONS.SHA.NODE_WIN_ARM64 : VERSIONS.SHA.NODE_WIN_X64;
  const root = toolRoot();
  const nodeDir = path.join(root, "node");
  if (fs.existsSync(path.join(nodeDir, "node.exe"))) {
    addToPath(nodeDir);
    if (nodeMajor() >= VERSIONS.NODE_MIN_MAJOR) return;
  }
  onProgress(`Downloading Node v${VERSIONS.NODE} (${arch})...`, 15);
  const zipPath = path.join(root, zipName);
  await downloadFile(url, zipPath);
  verifySha256(zipPath, expected);
  const extracted = path.join(root, `node-v${VERSIONS.NODE}-win-${arch}`);
  run("powershell", ["-NoProfile", "-Command", `Expand-Archive -Force -Path '${zipPath}' -DestinationPath '${root}'`], { shell: false });
  if (fs.existsSync(nodeDir)) fs.rmSync(nodeDir, { recursive: true, force: true });
  fs.renameSync(extracted, nodeDir);
  try { fs.unlinkSync(zipPath); } catch {}
  addToPath(nodeDir);
  if (nodeMajor() < VERSIONS.NODE_MIN_MAJOR) throw new Error("Portable Node install failed.");
}

async function ensurePortableGit(onProgress) {
  if (commandExists("git")) return;
  if (process.platform !== "win32") {
    throw new Error("'git' is required but not on PATH. Install git and relaunch.");
  }
  const arch = winArch();
  const suffix = arch === "arm64" ? "arm64" : "64-bit";
  const zipName = `MinGit-${VERSIONS.MINGIT}-${suffix}.zip`;
  const url = `https://github.com/git-for-windows/git/releases/download/v${VERSIONS.MINGIT}.windows.1/${zipName}`;
  const expected = arch === "arm64" ? VERSIONS.SHA.MINGIT_WIN_ARM64 : VERSIONS.SHA.MINGIT_WIN_X64;
  const root = toolRoot();
  const gitDir = path.join(root, "git");
  const gitExe = path.join(gitDir, "cmd", "git.exe");
  if (fs.existsSync(gitExe)) {
    addToPath(path.join(gitDir, "cmd"));
    if (commandExists("git")) return;
  }
  onProgress(`Downloading portable git ${VERSIONS.MINGIT}...`, 10);
  const zipPath = path.join(root, zipName);
  await downloadFile(url, zipPath);
  verifySha256(zipPath, expected);
  if (fs.existsSync(gitDir)) fs.rmSync(gitDir, { recursive: true, force: true });
  run("powershell", ["-NoProfile", "-Command", `Expand-Archive -Force -Path '${zipPath}' -DestinationPath '${gitDir}'`], { shell: false });
  try { fs.unlinkSync(zipPath); } catch {}
  addToPath(path.join(gitDir, "cmd"));
  if (!commandExists("git")) throw new Error("Portable git install failed.");
}

function ensureUv(onProgress) {
  if (commandExists("uv")) return;
  onProgress("Installing uv (Python toolchain)...", 20);
  if (process.platform === "win32") {
    run("powershell", ["-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", "irm https://astral.sh/uv/install.ps1 | iex"], { shell: false });
    addToPath(path.join(os.homedir(), ".local", "bin"));
    addToPath(path.join(os.homedir(), ".cargo", "bin"));
  } else {
    run("sh", ["-c", "curl -LsSf https://astral.sh/uv/install.sh | sh"], { shell: false });
    addToPath(path.join(os.homedir(), ".local", "bin"));
  }
  if (!commandExists("uv")) throw new Error("uv install failed -- add ~/.local/bin to PATH and relaunch.");
}

function cloneOrUpdate(dest, repoUrl, branch, onProgress) {
  fs.mkdirSync(path.dirname(dest), { recursive: true });
  if (fs.existsSync(path.join(dest, ".git"))) {
    onProgress(`Updating checkout (${branch})...`, 30);
    run("git", ["-C", dest, "fetch", "--no-tags", "origin", branch]);
    run("git", ["-C", dest, "checkout", branch]);
    const merge = spawnSync("git", ["-C", dest, "merge", "--ff-only", `origin/${branch}`], { encoding: "utf8", windowsHide: true });
    if (merge.status !== 0) {
      onProgress("Local changes present; skipped fast-forward.", 32);
    }
  } else {
    onProgress(`Cloning ${repoUrl}...`, 30);
    run("git", ["clone", "--branch", branch, repoUrl, dest]);
  }
}

function provisionPython(dest, onProgress) {
  onProgress("Provisioning Python via uv...", 45);
  run("uv", ["python", "install"], { cwd: dest });
  if (!fs.existsSync(path.join(dest, ".venv"))) {
    run("uv", ["venv", ".venv"], { cwd: dest });
  }
  onProgress("Installing Marionette + Puppetmaster...", 55);
  run("uv", ["pip", "install", "--python", ".venv", "-e", "."], { cwd: dest });
  const spec = process.env.MARIONETTE_PUPPETMASTER_SPEC || "puppetmaster-ai";
  run("uv", ["pip", "install", "--python", ".venv", spec], { cwd: dest });
}

function buildRenderer(dest, onProgress) {
  onProgress("Installing node deps + building renderer...", 70);
  runNpm(["ci"], { cwd: path.join(dest, "webapp"), inherit: true });
  runNpm(["run", "build"], { cwd: path.join(dest, "webapp"), inherit: true });
}

async function runBootstrap(targetDir, onProgress = () => {}) {
  const repoUrl = process.env.MARIONETTE_REPO_URL || DEFAULT_REPO;
  const branch = process.env.MARIONETTE_BRANCH || DEFAULT_BRANCH;

  onProgress("Checking prerequisites...", 5);
  // A Finder/Dock-launched app has a minimal PATH; hydrate it with Homebrew and
  // Node version-manager locations so node/git/uv are discoverable (fixes the
  // false "Node too old / not found" on machines with Homebrew Node).
  hydratePath();
  await ensurePortableGit(onProgress);
  ensureUv(onProgress);
  await ensurePortableNode(onProgress);

  cloneOrUpdate(targetDir, repoUrl, branch, onProgress);
  provisionPython(targetDir, onProgress);
  buildRenderer(targetDir, onProgress);

  onProgress("Bootstrap complete.", 100);
  if (!isInstallComplete(targetDir)) {
    throw new Error("Bootstrap finished but install validation failed.");
  }
}

// Windows: portable Node/MinGit live under %LOCALAPPDATA%\marionette\tools but
// addToPath() only mutates the CURRENT process env. First launch bootstraps and
// works; every later launch skips bootstrap (isInstallComplete), so git/npm
// children got ENOENT unless the user had system-wide installs. Re-inject the
// portable tool dirs on every startup.
function reinjectPortableTools() {
  if (process.platform !== "win32") return;
  try {
    const root = path.join(
      process.env.LOCALAPPDATA || path.join(os.homedir(), "AppData", "Local"),
      "marionette", "tools"
    );
    const nodeDir = path.join(root, "node");
    if (fs.existsSync(path.join(nodeDir, "node.exe"))) addToPath(nodeDir);
    const gitCmdDir = path.join(root, "git", "cmd");
    if (fs.existsSync(path.join(gitCmdDir, "git.exe"))) addToPath(gitCmdDir);
    addToPath(path.join(os.homedir(), ".local", "bin"));
  } catch { /* best-effort */ }
}

module.exports = { isInstallComplete, runBootstrap, venvPython, reinjectPortableTools, VERSIONS };
