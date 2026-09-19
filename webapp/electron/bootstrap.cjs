// Bootstrap a Marionette source checkout for the packaged thin Electron shell.
// When the app is installed from a release build (DMG/NSIS/AppImage), it does NOT
// bundle Python or a frozen backend. On first launch it clones the repo into
// ~/.marionette/release, provisions uv + node + git as needed, builds the venv
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
  UV: "0.12.16",
  SHA: {
    NODE_WIN_X64: "55b639295920b219bb2acbcfa00f90393a2789095b7323f79475c9f34795f217",
    NODE_WIN_ARM64: "2d71f5f9b2fffa33baa108c07d74b0d24e0c3dd8f441d567772ae0e3dd4b1a22",
    MINGIT_WIN_X64: "31497e7968196332263459ee319d2524e3ebc5786ab895e2abad34ffdd4f4ebf",
    MINGIT_WIN_ARM64: "377e283290e2de455cdd5cdbd99653bd911db752a8986d1ad914a5ac2fbd1192",
    UV_DARWIN_ARM64: "b6e03fae61704b1aa622f12b792a69483e837b83068e44f4fd34f8a07a8f74a3",
    UV_DARWIN_X64: "a42bcc9ce97eb8b364d7f162233a9c6b8c0ee25388e551d362809795127e0c31",
    UV_LINUX_ARM64: "36d913ee9c647481d64f1a0a0485f85ff2feaee605c341fc22e73398f9212c26",
    UV_LINUX_X64: "8e5c6e5523dffc2dcf615bd995554c84c9feb4e577808a3fb8698a639d3f8d9c",
    UV_WIN_X64: "f730454bf09019754e5e5abd71a8aa18683cb739cba0d9c720bac2e7c901160f",
    UV_WIN_ARM64: "9977129f89c4036edfcb200d2484755571e51fa74517f02e478c1d7bcc353b2e",
  },
};

const DEFAULT_REPO = "https://github.com/professorpalmer/marionette.git";
const crypto = require("node:crypto");
const RECEIPT = "marionette-bootstrap.json";

function usesDevelopmentCheckout(env = process.env) {
  return !!(env.MARIONETTE_CHECKOUT || env.HARNESS_CHECKOUT ||
    env.MARIONETTE_REPO_URL || env.MARIONETTE_BRANCH || env.MARIONETTE_REVISION ||
    env.PMHARNESS_DEV_SERVER || /^(1|true|yes)$/i.test(env.MARIONETTE_SELF_DEV || ""));
}

function selectPackagedCheckout({ env = process.env, home = os.homedir(), selfDev = false, selfDevCheckout = null } = {}) {
  const explicit = env.MARIONETTE_CHECKOUT || env.HARNESS_CHECKOUT;
  if (explicit) return explicit;
  if (selfDev && selfDevCheckout) return selfDevCheckout;
  // One reusable release checkout; never move, clean, or execute the legacy tree.
  return path.join(home, ".marionette", (selfDev || usesDevelopmentCheckout(env)) ? "marionette" : "release");
}

function bootstrapTarget(env = process.env) {
  const override = env.MARIONETTE_REPO_URL || env.MARIONETTE_BRANCH || env.MARIONETTE_REVISION;
  if (override) {
    if (env.MARIONETTE_REVISION && !/^[a-f0-9]{40}$/i.test(env.MARIONETTE_REVISION)) {
      throw new Error("MARIONETTE_REVISION must be a full Git commit SHA.");
    }
    return { mode: "development", repo: env.MARIONETTE_REPO_URL || DEFAULT_REPO,
      revision: env.MARIONETTE_REVISION ? env.MARIONETTE_REVISION.toLowerCase() : null, ref: env.MARIONETTE_BRANCH || "main" };
  }
  let metadata;
  try { metadata = JSON.parse(fs.readFileSync(path.join(__dirname, "bootstrap-revision.json"), "utf8")); }
  catch { throw new Error("Packaged source revision is missing. Reinstall a complete Marionette installer, or explicitly set MARIONETTE_REVISION for development."); }
  if (metadata.schema !== 1 || !/^[a-f0-9]{40}$/.test(metadata.revision) || metadata.repo !== DEFAULT_REPO) {
    throw new Error("Invalid packaged source revision. Reinstall Marionette.");
  }
  return { mode: "packaged", repo: metadata.repo, revision: metadata.revision };
}

function gitValue(dir, args) {
  const result = spawnSync("git", ["-C", dir, ...args], { encoding: "utf8", windowsHide: true });
  if (result.status !== 0) throw new Error(`Cannot validate checkout at ${dir}: ${result.stderr || "git unavailable"}`);
  return result.stdout.trim();
}

function receiptPath(dir) {
  return path.resolve(dir, gitValue(dir, ["rev-parse", "--git-path", RECEIPT]));
}

function checkoutPorcelain(dir) {
  return gitValue(dir, ["status", "--porcelain", "--untracked-files=all", "--no-renames"]);
}

function assertOrigin(dir, target) {
  if (gitValue(dir, ["remote", "get-url", "origin"]) !== target.repo) {
    throw new Error(`Checkout origin differs from ${target.repo}. Preserve ${dir} and choose a separate checkout or an explicit MARIONETTE_REPO_URL override.`);
  }
}

function assertCheckout(dir, target) {
  assertOrigin(dir, target);
  if (gitValue(dir, ["status", "--porcelain", "--untracked-files=all"])) {
    throw new Error(`Local changes in ${dir}. Commit or move your changes before relaunching; bootstrap will not reset or discard them.`);
  }
}

function isPackagedReleaseSlot(dir, target) {
  return target.mode === "packaged" && path.basename(dir) === "release";
}

function preserveReleaseDirt(dir) {
  const porcelain = checkoutPorcelain(dir);
  if (!porcelain) return null;
  const stamp = new Date().toISOString().replace(/[:.]/g, "-");
  const dest = path.join(path.dirname(dir), "hotfix-stash", stamp);
  fs.mkdirSync(dest, { recursive: true });
  fs.writeFileSync(path.join(dest, "STATUS.txt"), porcelain + "\n");
  try {
    fs.writeFileSync(path.join(dest, "HEAD.txt"), gitValue(dir, ["rev-parse", "HEAD"]) + "\n");
  } catch { /* unborn checkout */ }
  const names = new Set();
  for (const args of [
    ["diff", "--name-only", "HEAD"],
    ["diff", "--name-only", "--cached"],
    ["ls-files", "--others", "--exclude-standard"],
  ]) {
    try {
      for (const rel of gitValue(dir, args).split("\n")) {
        if (rel) names.add(rel);
      }
    } catch { /* list what we can */ }
  }
  for (const rel of names) {
    const src = path.join(dir, rel);
    if (!fs.existsSync(src)) continue;
    const out = path.join(dest, rel);
    fs.mkdirSync(path.dirname(out), { recursive: true });
    fs.cpSync(src, out, { recursive: true });
  }
  return dest;
}

function gitChecked(dir, args) {
  const result = spawnSync("git", ["-C", dir, ...args], { encoding: "utf8", windowsHide: true });
  if (result.status !== 0) {
    throw new Error(`Cannot update checkout at ${dir}: ${(result.stderr || result.stdout || "git failed").trim()}`);
  }
  return result.stdout.trim();
}

// Packaged ~/.marionette/release is installer-owned. Leftover tracked
// edits from a prior pin must not brick first-run; copy them aside, then
// reset so the embedded SHA can check out. Development checkouts still
// refuse dirty trees.
function preparePackagedReleaseCheckout(dir, target) {
  assertOrigin(dir, target);
  if (!checkoutPorcelain(dir)) return null;
  const stashDir = preserveReleaseDirt(dir);
  gitChecked(dir, ["reset", "--hard", "HEAD"]);
  gitChecked(dir, ["clean", "-fd"]);
  if (checkoutPorcelain(dir)) {
    throw new Error(
      `Local changes in ${dir} could not be moved aside` +
      (stashDir ? ` to ${stashDir}` : "") +
      "."
    );
  }
  return stashDir;
}

function installIdentity(dir, target) {
  const inputs = ["webapp/package-lock.json", "webapp/package.json", "pyproject.toml"];
  const hash = crypto.createHash("sha256");
  for (const name of inputs) hash.update(name).update(fs.readFileSync(path.join(dir, name)));
  return { schema: 1, mode: target.mode, repo: target.repo,
    revision: gitValue(dir, ["rev-parse", "HEAD"]), inputs: hash.digest("hex"),
    platform: process.platform, arch: process.arch,
    puppetmaster: process.env.MARIONETTE_PUPPETMASTER_SPEC || "puppetmaster-ai==1.27.29" };
}

function venvPython(dir) {
  return process.platform === "win32"
    ? path.join(dir, ".venv", "Scripts", "python.exe")
    : path.join(dir, ".venv", "bin", "python");
}

function isInstallComplete(dir, target = null) {
  try {
    target = target || bootstrapTarget();
    // Branch overrides must resolve the remote again; they never certify a release.
    if (!target.revision) return false;
    assertCheckout(dir, target);
    if (gitValue(dir, ["rev-parse", "HEAD"]) !== target.revision) return false;
    const receipt = JSON.parse(fs.readFileSync(receiptPath(dir), "utf8"));
    return JSON.stringify(receipt) === JSON.stringify(installIdentity(dir, target)) &&
      fs.existsSync(venvPython(dir)) &&
      fs.existsSync(path.join(dir, "webapp", "node_modules")) &&
      fs.existsSync(path.join(dir, "webapp", "dist", "index.html"));
  } catch {
    return false;
  }
}

// Yield so Electron can paint the bootstrap window and flush progress IPC.
// Heavy first-run steps used to call spawnSync on the main thread, which froze
// the UI and made macOS report Marionette as hung (~20s spindump) on DMG launch.
function yieldEventLoop() {
  return new Promise((resolve) => setImmediate(resolve));
}

async function reportProgress(onProgress, message, pct) {
  try { onProgress(message, pct); } catch { /* progress UI must never throw */ }
  await yieldEventLoop();
}

// Async child runner — keeps the Electron main process responsive during
// git/uv/npm work. Prefer this for any step that can take more than a tick.
function runAsync(cmd, args, opts = {}) {
  return new Promise((resolve, reject) => {
    let child;
    try {
      child = spawn(cmd, args, {
        env: opts.env || process.env,
        cwd: opts.cwd,
        shell: opts.shell || false,
        windowsHide: true,
        stdio: ["ignore", "pipe", "pipe"],
      });
    } catch (e) {
      reject(e);
      return;
    }
    let stdout = "";
    let stderr = "";
    if (child.stdout) child.stdout.on("data", (buf) => { stdout += buf; });
    if (child.stderr) child.stderr.on("data", (buf) => { stderr += buf; });
    child.on("error", reject);
    child.on("close", (code) => {
      if (code === 0) {
        resolve({ status: 0, stdout, stderr });
        return;
      }
      const detail = (stderr || stdout || "").trim();
      reject(new Error(
        `${cmd} ${args.join(" ")} failed (code ${code})` +
        `${detail ? ": " + detail.slice(0, 500) : ""}`
      ));
    });
  });
}

// On Windows, npm is a .cmd batch shim, not an executable. Node (post
// CVE-2024-27980) refuses to spawn .cmd files without shell:true, and the
// failed spawn surfaces as status:null -- the "npm ci failed (code null)"
// first-run error. Route npm through a shell on win32 only.
function runNpmAsync(args, opts = {}) {
  if (process.platform === "win32") {
    return runAsync("npm.cmd", args, { ...opts, shell: true });
  }
  return runAsync("npm", args, opts);
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
  await reportProgress(onProgress, `Downloading Node v${VERSIONS.NODE} (${arch})...`, 15);
  const zipPath = path.join(root, zipName);
  await downloadFile(url, zipPath);
  verifySha256(zipPath, expected);
  const extracted = path.join(root, `node-v${VERSIONS.NODE}-win-${arch}`);
  await runAsync("powershell", ["-NoProfile", "-Command", `Expand-Archive -Force -Path '${zipPath}' -DestinationPath '${root}'`], { shell: false });
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
  await reportProgress(onProgress, `Downloading portable git ${VERSIONS.MINGIT}...`, 10);
  const zipPath = path.join(root, zipName);
  await downloadFile(url, zipPath);
  verifySha256(zipPath, expected);
  if (fs.existsSync(gitDir)) fs.rmSync(gitDir, { recursive: true, force: true });
  await runAsync("powershell", ["-NoProfile", "-Command", `Expand-Archive -Force -Path '${zipPath}' -DestinationPath '${gitDir}'`], { shell: false });
  try { fs.unlinkSync(zipPath); } catch {}
  addToPath(path.join(gitDir, "cmd"));
  if (!commandExists("git")) throw new Error("Portable git install failed.");
}

function uvRelease() {
  const version = VERSIONS.UV;
  if (process.platform === "win32") {
    const target = process.arch === "arm64" ? "aarch64-pc-windows-msvc" : "x86_64-pc-windows-msvc";
    const sha = process.arch === "arm64" ? VERSIONS.SHA.UV_WIN_ARM64 : VERSIONS.SHA.UV_WIN_X64;
    return { version, target, sha, ext: "zip" };
  }
  if (process.platform === "darwin") {
    const target = process.arch === "arm64" ? "aarch64-apple-darwin" : "x86_64-apple-darwin";
    const sha = process.arch === "arm64" ? VERSIONS.SHA.UV_DARWIN_ARM64 : VERSIONS.SHA.UV_DARWIN_X64;
    return { version, target, sha, ext: "tar.gz" };
  }
  if (process.platform === "linux") {
    const target = process.arch === "arm64" ? "aarch64-unknown-linux-gnu" : "x86_64-unknown-linux-gnu";
    const sha = process.arch === "arm64" ? VERSIONS.SHA.UV_LINUX_ARM64 : VERSIONS.SHA.UV_LINUX_X64;
    return { version, target, sha, ext: "tar.gz" };
  }
  throw new Error(`no pinned uv build for ${process.platform}/${process.arch}`);
}

async function ensureUv(onProgress) {
  if (commandExists("uv")) return;
  const { version, target, sha, ext } = uvRelease();
  if (!sha) throw new Error(`no SHA256 pinned for uv ${target}`);
  const destDir = path.join(os.homedir(), ".local", "bin");
  fs.mkdirSync(destDir, { recursive: true });
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "marionette-uv-"));
  const archive = path.join(tmp, `uv.${ext}`);
  const url = `https://github.com/astral-sh/uv/releases/download/${version}/uv-${target}.${ext}`;
  await reportProgress(onProgress, `Downloading uv ${version} (${target})...`, 20);
  try {
    await downloadFile(url, archive);
    verifySha256(archive, sha);
    if (ext === "zip") {
      await runAsync("powershell", [
        "-NoProfile", "-Command",
        `Expand-Archive -Force -Path '${archive}' -DestinationPath '${tmp}'`,
      ], { shell: false });
    } else {
      await runAsync("tar", ["-xzf", archive, "-C", tmp], { shell: false });
    }
    const names = process.platform === "win32" ? ["uv.exe", "uvx.exe"] : ["uv", "uvx"];
    for (const name of names) {
      const found = findFile(tmp, name);
      if (!found) {
        if (name.startsWith("uvx")) continue;
        throw new Error(`${name} not found inside the verified uv archive`);
      }
      fs.copyFileSync(found, path.join(destDir, name));
      if (process.platform !== "win32") fs.chmodSync(path.join(destDir, name), 0o755);
    }
  } finally {
    fs.rmSync(tmp, { recursive: true, force: true });
  }
  addToPath(destDir);
  if (!commandExists("uv")) throw new Error("uv install failed -- add ~/.local/bin to PATH and relaunch.");
}

function findFile(root, name) {
  const stack = [root];
  while (stack.length) {
    const dir = stack.pop();
    let entries;
    try { entries = fs.readdirSync(dir, { withFileTypes: true }); } catch { continue; }
    for (const entry of entries) {
      const full = path.join(dir, entry.name);
      if (entry.isDirectory()) stack.push(full);
      else if (entry.name === name) return full;
    }
  }
  return "";
}

async function cloneOrUpdate(dest, target, onProgress) {
  fs.mkdirSync(path.dirname(dest), { recursive: true });
  if (fs.existsSync(path.join(dest, ".git"))) {
    if (isPackagedReleaseSlot(dest, target)) {
      const stashDir = preparePackagedReleaseCheckout(dest, target);
      if (stashDir) {
        await reportProgress(onProgress, `Preserved leftover checkout changes in ${stashDir}`, 28);
      }
    } else {
      assertCheckout(dest, target);
    }
  } else {
    if (fs.existsSync(dest) && fs.readdirSync(dest).length) {
      throw new Error(`Checkout directory ${dest} is not empty. Move it aside or choose a separate checkout; bootstrap will not discard files.`);
    }
    await reportProgress(onProgress, `Preparing checkout from ${target.repo}...`, 30);
    await runAsync("git", ["init", dest]);
    await runAsync("git", ["-C", dest, "remote", "add", "origin", target.repo]);
  }
  await reportProgress(onProgress, `Preparing ${target.mode} source (${target.revision || target.ref})...`, 32);
  await runAsync("git", ["-C", dest, "fetch", "--no-tags", "origin", target.revision || target.ref]);
  const revision = gitValue(dest, ["rev-parse", "FETCH_HEAD^{commit}"]);
  if (target.revision && revision !== target.revision) throw new Error("Fetched source does not match the requested commit.");
  await runAsync("git", ["-C", dest, "checkout", "--detach", revision]);
  return { ...target, revision };
}

async function provisionPython(dest, onProgress) {
  await reportProgress(onProgress, "Provisioning Python via uv...", 45);
  await runAsync("uv", ["python", "install"], { cwd: dest });
  if (!fs.existsSync(path.join(dest, ".venv"))) {
    await runAsync("uv", ["venv", ".venv"], { cwd: dest });
  }
  await reportProgress(onProgress, "Installing Marionette + Puppetmaster...", 55);
  await runAsync("uv", ["pip", "install", "--python", ".venv", "-e", "."], { cwd: dest });
  const spec = process.env.MARIONETTE_PUPPETMASTER_SPEC || "puppetmaster-ai==1.27.29";
  await runAsync("uv", ["pip", "install", "--python", ".venv", spec], { cwd: dest });
}

async function buildRenderer(dest, onProgress) {
  const webapp = path.join(dest, "webapp");
  await reportProgress(onProgress, "Installing node deps...", 70);
  await runNpmAsync(["ci"], { cwd: webapp });
  await reportProgress(onProgress, "Building renderer...", 85);
  await runNpmAsync(["run", "build"], { cwd: webapp });
}

async function runBootstrap(targetDir, onProgress = () => {}) {
  const target = bootstrapTarget();
  await reportProgress(onProgress, "Checking prerequisites...", 5);
  hydratePath();
  await ensurePortableGit(onProgress);
  if (isInstallComplete(targetDir, target)) return;
  const resolved = await cloneOrUpdate(targetDir, target, onProgress);
  const receipt = receiptPath(targetDir);
  // A failed retry cannot inherit success from an earlier install.
  fs.rmSync(receipt, { force: true });
  const identity = installIdentity(targetDir, resolved);
  await ensureUv(onProgress);
  await ensurePortableNode(onProgress);
  await provisionPython(targetDir, onProgress);
  await buildRenderer(targetDir, onProgress);
  assertCheckout(targetDir, resolved);
  if (JSON.stringify(identity) !== JSON.stringify(installIdentity(targetDir, resolved)) ||
      !fs.existsSync(venvPython(targetDir)) ||
      !fs.existsSync(path.join(targetDir, "webapp", "node_modules")) ||
      !fs.existsSync(path.join(targetDir, "webapp", "dist", "index.html"))) {
    throw new Error("Bootstrap finished but install validation failed.");
  }
  fs.writeFileSync(receipt + ".tmp", JSON.stringify(identity));
  fs.renameSync(receipt + ".tmp", receipt);
  await reportProgress(onProgress, `Bootstrap complete (${resolved.mode}, ${resolved.revision}).`, 100);
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

module.exports = {
  selectPackagedCheckout,
  usesDevelopmentCheckout,
  bootstrapTarget,
  isInstallComplete,
  runBootstrap,
  venvPython,
  reinjectPortableTools,
  VERSIONS,
  runAsync, // exported for event-loop regression tests
};
