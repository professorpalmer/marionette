"use strict";

// Tests for the authenticated backend reuse probe (backend-probe.cjs).
// Regression: startBackend's reuse path used to accept ANY http answer on the
// marker port as "healthy", so a live backend holding a different token was
// adopted and every renderer request 403'd. Run with
// `node --test electron/*.test.cjs`.

const { test } = require("node:test");
const assert = require("node:assert/strict");
const http = require("node:http");

const {
  isAuthenticatedBackendHealthy,
  probeAuthenticatedBackend,
  waitForAuthenticatedBackend,
} = require("./backend-probe.cjs");

const GOOD_TOKEN = "feedface0123456789abcdef01234567";

function startTokenGatedServer() {
  const server = http.createServer((req, res) => {
    if (req.headers["x-harness-token"] === GOOD_TOKEN) {
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end('{"ok":true}');
    } else {
      res.writeHead(403, { "Content-Type": "application/json" });
      res.end('{"error":"missing or bad token"}');
    }
  });
  return new Promise((resolve) => {
    server.listen(0, "127.0.0.1", () => resolve({ server, port: server.address().port }));
  });
}

test("isAuthenticatedBackendHealthy: only 2xx counts", () => {
  assert.equal(isAuthenticatedBackendHealthy(200), true);
  assert.equal(isAuthenticatedBackendHealthy(204), true);
  assert.equal(isAuthenticatedBackendHealthy(403), false);
  assert.equal(isAuthenticatedBackendHealthy(500), false);
  assert.equal(isAuthenticatedBackendHealthy(undefined), false);
});

test("probe accepts a live backend when the candidate token authenticates", async () => {
  const { server, port } = await startTokenGatedServer();
  try {
    assert.equal(await probeAuthenticatedBackend({ port, token: GOOD_TOKEN }), true);
  } finally {
    server.close();
  }
});

test("probe rejects a live backend on token mismatch (403 is NOT healthy)", async () => {
  const { server, port } = await startTokenGatedServer();
  try {
    await assert.rejects(
      probeAuthenticatedBackend({ port, token: "wrong-token" }),
      (err) => {
        assert.equal(err.tokenRejected, true);
        assert.ok(!String(err.message).includes("wrong-token"), "error must not echo the token");
        assert.ok(!String(err.message).includes(GOOD_TOKEN), "error must not echo the token");
        return true;
      }
    );
  } finally {
    server.close();
  }
});

test("probe rejects a live backend when the candidate token is missing", async () => {
  const { server, port } = await startTokenGatedServer();
  try {
    await assert.rejects(
      probeAuthenticatedBackend({ port, token: "" }),
      (err) => err.tokenRejected === true
    );
  } finally {
    server.close();
  }
});

test("waitForAuthenticatedBackend: token rejection fails fast (no retry loop)", async () => {
  let attempts = 0;
  const rejectingProbe = async () => {
    attempts += 1;
    const err = new Error("backend rejected the candidate token (HTTP 403)");
    err.tokenRejected = true;
    throw err;
  };
  await assert.rejects(
    waitForAuthenticatedBackend({
      port: 1, token: "x", timeoutMs: 5000, probe: rejectingProbe, sleep: async () => {},
    }),
    /HTTP 403/
  );
  assert.equal(attempts, 1, "a definitive auth rejection must not be retried");
});

test("waitForAuthenticatedBackend: transient connection errors retry until deadline", async () => {
  let attempts = 0;
  const refusingProbe = async () => {
    attempts += 1;
    const err = new Error("connect ECONNREFUSED");
    err.code = "ECONNREFUSED";
    throw err;
  };
  await assert.rejects(
    waitForAuthenticatedBackend({
      port: 1, token: "x", timeoutMs: 50, probeIntervalMs: 10, probe: refusingProbe, sleep: async () => {},
    }),
    /ECONNREFUSED/
  );
  assert.ok(attempts > 1, "connection refusals should be retried before the deadline");
});

test("waitForAuthenticatedBackend: succeeds once the backend authenticates", async () => {
  let attempts = 0;
  const flakyProbe = async () => {
    attempts += 1;
    if (attempts < 3) {
      const err = new Error("connect ECONNREFUSED");
      err.code = "ECONNREFUSED";
      throw err;
    }
    return true;
  };
  assert.equal(
    await waitForAuthenticatedBackend({
      port: 1, token: "x", timeoutMs: 5000, probe: flakyProbe, sleep: async () => {},
    }),
    true
  );
  assert.equal(attempts, 3);
});
