"use strict";

const { test } = require("node:test");
const assert = require("node:assert/strict");
const { decideBackendPortRefresh } = require("./backend-marker.cjs");

test("decideBackendPortRefresh table", () => {
  const cases = [
    ["null raw -> no adopt", null, 5000, { adopt: false, port: null }],
    ["empty raw -> no adopt", "", 5000, { adopt: false, port: null }],
    ["bad JSON -> no adopt", "{nope", 5000, { adopt: false, port: null }],
    ["same port -> no adopt", JSON.stringify({ port: 49928 }), 49928, { adopt: false, port: 49928 }],
    ["different port -> adopt", JSON.stringify({ port: 49928, pid: 1 }), 65196, { adopt: true, port: 49928 }],
    ["missing port -> no adopt", JSON.stringify({ pid: 1 }), 5000, { adopt: false, port: null }],
  ];
  for (const [name, raw, current, expected] of cases) {
    assert.deepEqual(decideBackendPortRefresh(raw, current), expected, name);
  }
});
