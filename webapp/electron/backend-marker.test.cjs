"use strict";

const { test } = require("node:test");
const assert = require("node:assert/strict");
const { decideBackendPortRefresh } = require("./backend-marker.cjs");

test("decideBackendPortRefresh: null/empty raw -> no adopt", () => {
  assert.deepEqual(decideBackendPortRefresh(null, 5000), { adopt: false, port: null });
  assert.deepEqual(decideBackendPortRefresh("", 5000), { adopt: false, port: null });
});

test("decideBackendPortRefresh: bad JSON -> no adopt", () => {
  assert.deepEqual(decideBackendPortRefresh("{nope", 5000), { adopt: false, port: null });
});

test("decideBackendPortRefresh: same port -> no adopt", () => {
  assert.deepEqual(
    decideBackendPortRefresh(JSON.stringify({ port: 49928 }), 49928),
    { adopt: false, port: 49928 },
  );
});

test("decideBackendPortRefresh: different port -> adopt", () => {
  assert.deepEqual(
    decideBackendPortRefresh(JSON.stringify({ port: 49928, pid: 1 }), 65196),
    { adopt: true, port: 49928 },
  );
});

test("decideBackendPortRefresh: missing port -> no adopt", () => {
  assert.deepEqual(
    decideBackendPortRefresh(JSON.stringify({ pid: 1 }), 5000),
    { adopt: false, port: null },
  );
});
