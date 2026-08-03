"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");
const {
  currentBackendIdentity,
  parseBackendMarker,
  markerMatchesIdentity,
  decideBackendReuse,
  buildBackendMarkerPayload,
  liveIdentityMatches,
} = require("./backend-identity.cjs");

const EXPECTED = {
  repoRoot: "/Users/x/.marionette/marionette",
  checkoutSha: "9cbc8e8abcdef",
  packageVersion: "0.9.161",
  appVersion: "0.9.161",
};

test("parseBackendMarker: rejects bad shapes", () => {
  assert.equal(parseBackendMarker(null), null);
  assert.equal(parseBackendMarker("{"), null);
  assert.equal(parseBackendMarker(JSON.stringify({ pid: 1 })), null);
});

test("parseBackendMarker: accepts identity-bearing marker", () => {
  const m = parseBackendMarker(JSON.stringify({
    port: 52554,
    pid: 42,
    checkoutSha: "abc",
    packageVersion: "0.9.154",
    repoRoot: "/r",
  }));
  assert.equal(m.port, 52554);
  assert.equal(m.checkoutSha, "abc");
  assert.equal(m.packageVersion, "0.9.154");
});

test("markerMatchesIdentity: old markers without sha cannot be reused", () => {
  assert.equal(
    markerMatchesIdentity({ port: 1, checkoutSha: "" }, EXPECTED),
    false,
  );
});

test("markerMatchesIdentity: sha mismatch -> false", () => {
  assert.equal(
    markerMatchesIdentity({
      port: 1,
      checkoutSha: "d173bc8old",
      packageVersion: "0.9.154",
      repoRoot: EXPECTED.repoRoot,
    }, EXPECTED),
    false,
  );
});

test("markerMatchesIdentity: matching sha + root -> true", () => {
  assert.equal(
    markerMatchesIdentity({
      port: 1,
      checkoutSha: EXPECTED.checkoutSha,
      packageVersion: EXPECTED.packageVersion,
      repoRoot: EXPECTED.repoRoot,
    }, EXPECTED),
    true,
  );
});

test("decideBackendReuse: marker/version mismatch requests replace", () => {
  const verdict = decideBackendReuse({
    markerRaw: JSON.stringify({
      port: 52554,
      pid: 99,
      checkoutSha: "d173bc8old",
      packageVersion: "0.9.154",
      repoRoot: EXPECTED.repoRoot,
    }),
    expectedIdentity: EXPECTED,
    authenticated: true,
  });
  assert.equal(verdict.action, "replace");
  assert.equal(verdict.reason, "identity_mismatch");
  assert.equal(verdict.marker.port, 52554);
  assert.equal(verdict.marker.pid, 99);
});

test("decideBackendReuse: matching identity reuses", () => {
  const verdict = decideBackendReuse({
    markerRaw: JSON.stringify(buildBackendMarkerPayload({
      port: 52554,
      pid: 7,
      identity: EXPECTED,
      at: 1,
    })),
    expectedIdentity: EXPECTED,
    authenticated: true,
  });
  assert.equal(verdict.action, "reuse");
  assert.equal(verdict.reason, "identity_match");
});

test("decideBackendReuse: auth failure replaces even when identity matches", () => {
  const verdict = decideBackendReuse({
    markerRaw: JSON.stringify(buildBackendMarkerPayload({
      port: 1,
      pid: 2,
      identity: EXPECTED,
    })),
    expectedIdentity: EXPECTED,
    authenticated: false,
  });
  assert.equal(verdict.action, "replace");
  assert.equal(verdict.reason, "not_authenticated");
});

test("decideBackendReuse: missing marker spawns", () => {
  assert.equal(
    decideBackendReuse({ markerRaw: null, expectedIdentity: EXPECTED }).action,
    "spawn",
  );
});

test("liveIdentityMatches: handshake against /api/config payload", () => {
  assert.equal(
    liveIdentityMatches(
      { checkout_sha: EXPECTED.checkoutSha, package_version: "0.9.161" },
      EXPECTED,
    ),
    true,
  );
  assert.equal(
    liveIdentityMatches(
      { checkout_sha: "old", package_version: "0.9.161" },
      EXPECTED,
    ),
    false,
  );
});

test("currentBackendIdentity: injectable sha/version readers", () => {
  const id = currentBackendIdentity({
    repoRoot: "/tmp/repo",
    appVersion: "0.9.161",
    readSha: () => "deadbeef",
    readVersion: () => "0.9.161",
  });
  assert.equal(id.checkoutSha, "deadbeef");
  assert.equal(id.packageVersion, "0.9.161");
  assert.ok(id.repoRoot.includes("repo"));
});
