"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

test("main.cjs must not redeclare const path inside _startBackendOnce (TDZ crash)", () => {
  const src = fs.readFileSync(path.join(__dirname, "main.cjs"), "utf8");
  const start = src.indexOf("async function _startBackendOnce");
  assert.ok(start >= 0, "_startBackendOnce missing");
  const next = src.indexOf("\nasync function ", start + 1);
  const body = next >= 0 ? src.slice(start, next) : src.slice(start);
  assert.equal(
    /const\s+path\s*=\s*require\(\s*["']node:path["']\s*\)/.test(body),
    false,
    "local const path = require(\"node:path\") TDZ-shadows module path and breaks spawn",
  );
  assert.match(body, /const marionetteModels = path\.join\(/);
});
