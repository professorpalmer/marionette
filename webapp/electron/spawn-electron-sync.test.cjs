"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { spawnElectronSync } = require("./spawn-electron-sync.cjs");

test("spawnElectronSync retries ETXTBSY then returns success", () => {
  let n = 0;
  const result = spawnElectronSync("electron", [], {}, 5, () => {
    n += 1;
    if (n < 3) return { error: Object.assign(new Error("busy"), { code: "ETXTBSY" }), status: null };
    return { status: 0, error: null, stdout: "ok", stderr: "" };
  });
  assert.equal(n, 3);
  assert.equal(result.status, 0);
  assert.equal(result.stdout, "ok");
});
