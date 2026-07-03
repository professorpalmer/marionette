"use strict";

const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const bootstrap = require("./bootstrap.cjs");

test("isInstallComplete: false for empty directory", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "marionette-bootstrap-"));
  assert.equal(bootstrap.isInstallComplete(dir), false);
});

test("isInstallComplete: true when git, venv python, and dist exist", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "marionette-bootstrap-"));
  fs.mkdirSync(path.join(dir, ".git"), { recursive: true });
  const pyDir = process.platform === "win32"
    ? path.join(dir, ".venv", "Scripts")
    : path.join(dir, ".venv", "bin");
  fs.mkdirSync(pyDir, { recursive: true });
  const pyName = process.platform === "win32" ? "python.exe" : "python";
  fs.writeFileSync(path.join(pyDir, pyName), "");
  fs.mkdirSync(path.join(dir, "webapp", "dist"), { recursive: true });
  fs.writeFileSync(path.join(dir, "webapp", "dist", "index.html"), "<html></html>");
  assert.equal(bootstrap.isInstallComplete(dir), true);
});

test("venvPython: platform-specific path", () => {
  const dir = "/tmp/marionette";
  const py = bootstrap.venvPython(dir);
  if (process.platform === "win32") {
    assert.match(py, /Scripts\\python\.exe$/);
  } else {
    assert.match(py, /bin\/python$/);
  }
});

test("VERSIONS pins match expected Node minimum", () => {
  assert.ok(bootstrap.VERSIONS.NODE_MIN_MAJOR >= 20);
  assert.match(bootstrap.VERSIONS.NODE, /^\d+\.\d+\.\d+$/);
});
