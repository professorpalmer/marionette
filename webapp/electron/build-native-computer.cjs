"use strict";

const fs = require("node:fs");
const path = require("node:path");
const crypto = require("node:crypto");
const { execFile } = require("node:child_process");

const sourcePath = path.join(__dirname, "native", "computer-macos.swift");
const speechSourcePath = path.join(__dirname, "native", "speech-macos.swift");
const computerFrameworks = ["AppKit", "ApplicationServices", "ScreenCaptureKit"];
const outputDir = path.join(__dirname, "native", "bin");
const packagedHelperPaths = Object.freeze({
  darwin: path.join("native-computer", "computer-macos"),
  win32: path.join("native-computer", "computer-windows.ps1"),
});
const packagedSpeechHelperPath = path.join("native-speech", "speech-macos");

function run(file, args, env = process.env) {
  return new Promise((resolve, reject) => {
    execFile(file, args, { maxBuffer: 1024 * 1024, env }, (error, stdout, stderr) => {
      if (error) {
        error.message = `${file} failed: ${stderr.trim() || error.message}`;
        reject(error);
      } else resolve(stdout);
    });
  });
}

function frameworkArgs(frameworks) {
  return frameworks.flatMap(name => ["-framework", name]);
}

async function compileDevelopmentMacHelper(destination, source = sourcePath, frameworks = computerFrameworks) {
  const fingerprint = crypto.createHash("sha256").update(fs.readFileSync(source)).update(process.arch).digest("hex");
  const stamp = `${destination}.sha256`;
  if (fs.existsSync(destination) && fs.existsSync(stamp) && fs.readFileSync(stamp, "utf8") === fingerprint) return destination;
  fs.mkdirSync(path.dirname(destination), { recursive: true, mode: 0o700 });
  await compileSwift(["-O", source, "-o", destination, ...frameworkArgs(frameworks)], path.join(path.dirname(destination), "module-cache"));
  fs.chmodSync(destination, 0o700);
  fs.writeFileSync(stamp, fingerprint, { mode: 0o600 });
  return destination;
}

/** Each Xcode compiles with its own swiftc: an older compiler cannot read a newer SDK. */
function xcodeDeveloperDirs() {
  try {
    return fs.readdirSync("/Applications")
      .filter(name => /^Xcode.*\.app$/.test(name))
      .sort().reverse()
      .map(name => path.join("/Applications", name, "Contents/Developer"));
  } catch { return []; }
}

function sdkCandidates() {
  const roots = [
    "/Library/Developer/CommandLineTools/SDKs",
    "/Applications/Xcode.app/Contents/Developer/Platforms/MacOSX.platform/Developer/SDKs",
  ];
  const sdks = roots.flatMap(root => {
    try { return fs.readdirSync(root).filter(name => /^MacOSX[\d.]+\.sdk$/.test(name)).sort().reverse().map(name => ({ sdk: path.join(root, name) })); }
    catch { return []; }
  });
  return [{}, ...sdks, ...xcodeDeveloperDirs().map(developerDir => ({ developerDir }))];
}

function toolchainEnv(candidate) {
  return candidate.developerDir ? { ...process.env, DEVELOPER_DIR: candidate.developerDir } : process.env;
}

async function compileSwift(args, cacheRoot, candidates = sdkCandidates()) {
  const errors = [];
  for (let index = 0; index < candidates.length; index++) {
    const candidate = candidates[index];
    const cache = path.join(cacheRoot, String(index));
    fs.mkdirSync(cache, { recursive: true, mode: 0o700 });
    const env = toolchainEnv(candidate);
    try {
      await run("xcrun", ["swiftc", ...(candidate.sdk ? ["-sdk", candidate.sdk] : []), "-module-cache-path", cache, ...args], env);
      return candidate;
    } catch (error) { errors.push(`[${candidate.developerDir || candidate.sdk || "default"}] ${error.message.split("\n")[0]}`); }
  }
  throw new Error(`no SDK compiled it:\n${errors.join("\n")}`);
}

async function buildUniversalMacHelper(destination = path.join(outputDir, "computer-macos"), source = sourcePath, frameworks = computerFrameworks) {
  fs.mkdirSync(path.dirname(destination), { recursive: true });
  const work = fs.mkdtempSync(path.join(path.dirname(destination), ".native-computer-"));
  try {
    const arm64 = path.join(work, "helper-arm64");
    const x64 = path.join(work, "helper-x86_64");
    const common = ["-O", source, ...frameworkArgs(frameworks)];
    const toolchain = await compileSwift([...common, "-target", "arm64-apple-macos14.0", "-o", arm64], path.join(work, "module-cache-arm64"));
    await compileSwift([...common, "-target", "x86_64-apple-macos14.0", "-o", x64], path.join(work, "module-cache-x64"), [toolchain]);
    await run("xcrun", ["lipo", "-create", arm64, x64, "-output", destination], toolchainEnv(toolchain));
    fs.chmodSync(destination, 0o755);
    return destination;
  } finally {
    fs.rmSync(work, { recursive: true, force: true });
  }
}

if (require.main === module) {
  buildUniversalMacHelper(process.argv[2]).then(
    destination => process.stdout.write(`${destination}\n`),
    error => { process.stderr.write(`${error.message}\n`); process.exitCode = 1; },
  );
}

/** The speech helper needs the macOS 26 SDK (SpeechAnalyzer). It runs on older macOS and reports that it is unavailable. */
function buildUniversalSpeechHelper(destination = path.join(outputDir, "speech-macos")) {
  return buildUniversalMacHelper(destination, speechSourcePath, []);
}

module.exports = {
  buildUniversalMacHelper,
  buildUniversalSpeechHelper,
  compileDevelopmentMacHelper,
  packagedHelperPaths,
  packagedSpeechHelperPath,
  sourcePath,
  speechSourcePath,
};
