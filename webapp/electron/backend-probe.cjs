"use strict";

// Authenticated backend reuse probe.
//
// startBackend() reuses a live backend found via the backend.json marker instead
// of spawning a second process on the same SQLite state. The old liveness check
// (any HTTP answer on /api/config, no token) accepted a backend we could not
// actually talk to: after an update relaunch or crash, a stale backend holding an
// OLD token could sit on the marker port, get adopted as "healthy", and then 403
// every renderer request (v0.9.95 update-skew incident). Reuse must prove the
// candidate token AUTHENTICATES -- a live-but-unauthorized backend is not
// reusable, and the caller should spawn a fresh one instead.

const http = require("node:http");

/** Only a 2xx proves the candidate token works; a 403 proves it does NOT. */
function isAuthenticatedBackendHealthy(statusCode) {
  return Number.isInteger(statusCode) && statusCode >= 200 && statusCode < 300;
}

/**
 * One authenticated probe of GET /api/config with the candidate token.
 * Resolves true on 2xx. Rejects otherwise; a definitive auth rejection is
 * marked `err.tokenRejected = true` so callers stop retrying immediately.
 * Error text carries only the status -- never the token.
 */
function probeAuthenticatedBackend({ port, token, timeoutMs = 2000, host = "127.0.0.1" }) {
  return new Promise((resolve, reject) => {
    const req = http.get({
      host,
      port,
      path: "/api/config",
      headers: token ? { "X-Harness-Token": token } : {},
      timeout: timeoutMs,
    }, (res) => {
      res.resume(); // drain; the body is irrelevant to the health verdict
      if (isAuthenticatedBackendHealthy(res.statusCode)) return resolve(true);
      const err = new Error(
        `backend on port ${port} rejected the candidate token (HTTP ${res.statusCode})`
      );
      err.tokenRejected = true;
      reject(err);
    });
    req.on("error", reject);
    req.on("timeout", () => { req.destroy(new Error("authenticated backend probe timed out")); });
  });
}

/**
 * Poll the probe until it authenticates or the deadline passes. Transient
 * connection errors retry (backend may still be binding); a token rejection is
 * definitive and fails immediately -- retrying a wrong token cannot help.
 * `probe`/`sleep` are injectable for tests.
 */
async function waitForAuthenticatedBackend({
  port,
  token,
  timeoutMs = 2000,
  probeIntervalMs = 300,
  probe = probeAuthenticatedBackend,
  sleep = (ms) => new Promise((r) => setTimeout(r, ms)),
}) {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    try {
      return await probe({ port, token, timeoutMs });
    } catch (err) {
      if (err && err.tokenRejected) throw err;
      if (Date.now() >= deadline) throw err;
      await sleep(probeIntervalMs);
    }
  }
}

module.exports = {
  isAuthenticatedBackendHealthy,
  probeAuthenticatedBackend,
  waitForAuthenticatedBackend,
};
