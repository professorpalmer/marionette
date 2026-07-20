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
