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

function run(file, args) {
  return new Promise((resolve, reject) => {
    execFile(file, args, { maxBuffer: 1024 * 1024 }, (error, stdout, stderr) => {
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

function xcodeSdkRoots() {
  try {
    return fs.readdirSync("/Applications")
      .filter(name => /^Xcode.*\.app$/.test(name))
      .sort().reverse()
      .map(name => path.join("/Applications", name, "Contents/Developer/Platforms/MacOSX.platform/Developer/SDKs"));
  } catch { return []; }
}

function sdkCandidates() {
  const roots = [
    "/Library/Developer/CommandLineTools/SDKs",
    "/Applications/Xcode.app/Contents/Developer/Platforms/MacOSX.platform/Developer/SDKs",
    ...xcodeSdkRoots(),
  ];
  return [null, ...roots.flatMap(root => {
    try { return fs.readdirSync(root).filter(name => /^MacOSX[\d.]+\.sdk$/.test(name)).sort().reverse().map(name => path.join(root, name)); }
    catch { return []; }
  })];
}

async function compileSwift(args, cacheRoot, candidates = sdkCandidates()) {
  let lastError;
  for (let index = 0; index < candidates.length; index++) {
    const sdk = candidates[index];
    const cache = path.join(cacheRoot, String(index));
    fs.mkdirSync(cache, { recursive: true, mode: 0o700 });
    try {
      await run("xcrun", ["swiftc", ...(sdk ? ["-sdk", sdk] : []), "-module-cache-path", cache, ...args]);
      return sdk;
    } catch (error) { lastError = error; }
  }
  throw lastError;
}

async function buildUniversalMacHelper(destination = path.join(outputDir, "computer-macos"), source = sourcePath, frameworks = computerFrameworks) {
  fs.mkdirSync(path.dirname(destination), { recursive: true });
  const work = fs.mkdtempSync(path.join(path.dirname(destination), ".native-computer-"));
  try {
    const arm64 = path.join(work, "helper-arm64");
    const x64 = path.join(work, "helper-x86_64");
    const common = ["-O", source, ...frameworkArgs(frameworks)];
    const sdk = await compileSwift([...common, "-target", "arm64-apple-macos14.0", "-o", arm64], path.join(work, "module-cache-arm64"));
    await compileSwift([...common, "-target", "x86_64-apple-macos14.0", "-o", x64], path.join(work, "module-cache-x64"), [sdk]);
    await run("xcrun", ["lipo", "-create", arm64, x64, "-output", destination]);
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
