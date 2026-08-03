"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const webappDir = path.resolve(__dirname, "..");

test("mac release uses electron-builder notarization exactly once", () => {
  const config = fs.readFileSync(
    path.join(webappDir, "electron-builder.yml"),
    "utf8",
  );
  const pkg = JSON.parse(
    fs.readFileSync(path.join(webappDir, "package.json"), "utf8"),
  );

  assert.doesNotMatch(config, /^\s*afterSign\s*:/m);
  assert.equal(
    fs.existsSync(path.join(webappDir, "build", "notarize.cjs")),
    false,
  );
  assert.equal(pkg.devDependencies?.["@electron/notarize"], undefined);
  assert.ok(pkg.devDependencies?.["electron-builder"]);
});

test("release config publishes GitHub updater metadata for electron-updater", () => {
  const config = fs.readFileSync(
    path.join(webappDir, "electron-builder.yml"),
    "utf8",
  );
  const pkg = JSON.parse(
    fs.readFileSync(path.join(webappDir, "package.json"), "utf8"),
  );

  assert.match(config, /^\s*publish\s*:/m);
  assert.match(config, /^\s*provider\s*:\s*github\s*$/m);
  assert.match(config, /^\s*owner\s*:\s*professorpalmer\s*$/m);
  assert.match(config, /^\s*repo\s*:\s*marionette\s*$/m);
  // mac zip target is required so electron-builder emits latest-mac.yml.
  assert.match(config, /target:\s*zip/);
  assert.ok(
    pkg.dependencies?.["electron-updater"] || pkg.devDependencies?.["electron-updater"],
    "electron-updater must be a package dependency for packaged installs",
  );
});
