"use strict";
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const assert = require("node:assert/strict");
const { spawn, execFileSync } = require("node:child_process");
const { createNativeComputer } = require("./native-computer.cjs");

async function main() {
  if (process.platform !== "win32") throw new Error("Windows smoke requires Windows.");
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "mar-native-windows-"));
  const fixture = path.join(root, "MarionetteNativeFixture.exe");
  execFileSync("powershell.exe", ["-NoProfile", "-NonInteractive", "-File", path.join(__dirname, "native", "computer-fixture-windows.ps1"), "-Destination", fixture], { timeout: 60000 });
  const child = spawn(fixture, [], { stdio: "ignore" });
  const native = createNativeComputer({ app: { isPackaged: false }, userDataPath: root });
  const app_id = "exe:" + fixture.toLowerCase();
  try {
    assert.equal((await native.dispatch({ operation: "status" })).supported, true);
    let state;
    for (let attempt = 0; attempt < 50; attempt++) {
      try { state = await native.dispatch({ operation: "snapshot", app_id }); break; }
      catch (error) { if (attempt === 49) throw error; await new Promise(resolve => setTimeout(resolve, 100)); }
    }
    assert.doesNotMatch(JSON.stringify(state), /NEVER_SNAPSHOT_PASSWORD/);
    assert.ok(fs.existsSync(state.screenshot_path));
    assert.ok((await native.dispatch({ operation: "apps" })).apps.some(app => app.app_id === app_id));
    const input = state.elements.find(row => row.label === "Fixture name");
    assert.ok(input);
    await native.dispatch({ operation: "type", app_id, snapshot_id: state.snapshot_id, ref: input.ref, text: "Marionette" });
    await assert.rejects(native.dispatch({ operation: "click", app_id, snapshot_id: state.snapshot_id, ref: input.ref }), /Stale/);
    state = await native.dispatch({ operation: "snapshot", app_id });
    const button = state.elements.find(row => row.label === "Apply fixture");
    assert.ok(button);
    await native.dispatch({ operation: "click", app_id, snapshot_id: state.snapshot_id, ref: button.ref });
    state = await native.dispatch({ operation: "snapshot", app_id });
    assert.match(state.text, /Saved Marionette/);
    console.log("PASS: Windows native UI Automation, scoped capture, real input, verification, password exclusion, stale refs.");
  } finally { native.close(); child.kill(); }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
