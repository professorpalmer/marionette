// Explicit local smoke: creates and controls only its disposable fixture app.
"use strict";
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const assert = require("node:assert/strict");
const { spawn, execFileSync } = require("node:child_process");
const { _test } = require("./native-computer.cjs");

async function main() {
  if (process.platform !== "darwin") throw new Error("This fixture smoke requires macOS.");
  const helper = process.argv[2];
  if (!helper || !fs.existsSync(helper)) throw new Error("Pass the freshly built native helper path.");
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "mar-native-smoke-"));
  const bundle = path.join(root, "Marionette Computer Fixture.app", "Contents");
  fs.mkdirSync(path.join(bundle, "MacOS"), { recursive: true });
  fs.writeFileSync(path.join(bundle, "Info.plist"), `<?xml version="1.0"?><plist version="1.0"><dict><key>CFBundleIdentifier</key><string>com.marionette.computer-fixture</string><key>CFBundleName</key><string>Marionette Computer Fixture</string><key>CFBundleExecutable</key><string>fixture</string><key>CFBundlePackageType</key><string>APPL</string></dict></plist>`);
  const executable = path.join(bundle, "MacOS", "fixture");
  execFileSync("xcrun", ["swiftc", "-sdk", process.env.SMOKE_MAC_SDK || execFileSync("xcrun", ["--show-sdk-path"], { encoding: "utf8" }).trim(), "-module-cache-path", path.join(root, "module-cache"), path.join(__dirname, "native", "computer-fixture-macos.swift"), "-o", executable, "-framework", "AppKit"], { timeout: 60000 });
  const child = spawn(executable, [], { stdio: "ignore" });
  const screenshots = path.join(root, "screenshots");
  const client = _test.createProtocolClient({ file: helper, args: ["--screenshot-dir", screenshots] });
  try {
    const status = await client.request({ operation: "status" });
    assert.equal(status.accessibility, true, "Enable Accessibility for the launching app before running this smoke.");
    assert.equal(status.screenRecording, true, "Enable Screen Recording for the launching app before running this smoke.");
    const app_id = "bundle:com.marionette.computer-fixture";
    let state;
    for (let attempt = 0; attempt < 50; attempt++) {
      try { state = await client.request({ operation: "snapshot", app_id }); break; }
      catch (error) { if (attempt === 49) throw error; await new Promise(resolve => setTimeout(resolve, 100)); }
    }
    assert.doesNotMatch(JSON.stringify(state), /NEVER_SNAPSHOT_PASSWORD/);
    assert.ok(fs.existsSync(state.screenshot_path));
    const input = state.elements.find(row => row.label === "Fixture name");
    assert.ok(input, "fixture text field must be discoverable");
    await client.request({ operation: "type", app_id, snapshot_id: state.snapshot_id, ref: input.ref, text: "Marionette" });
    await assert.rejects(client.request({ operation: "type", app_id, snapshot_id: state.snapshot_id, ref: input.ref, text: "duplicate" }), /Stale/);
    state = await client.request({ operation: "snapshot", app_id });
    const button = state.elements.find(row => row.label === "Apply fixture");
    await client.request({ operation: "click", app_id, snapshot_id: state.snapshot_id, ref: button.ref });
    state = await client.request({ operation: "snapshot", app_id });
    assert.match(state.text, /Saved Marionette/);
    console.log("PASS: native AX discovery, password exclusion, screenshot, real typing/click, fresh-state verification, stale-ref rejection.");
    console.log("Fixture evidence: " + state.screenshot_path);
  } finally { client.close(); child.kill(); }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
