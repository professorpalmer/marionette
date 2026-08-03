"use strict";

// Backend source-identity handshake helpers.
//
// After a source checkout update, Electron must not adopt a still-running
// backend that was spawned from an older tree (marker port + valid token is
// not enough). The marker records the checkout identity at spawn time; reuse
// compares that to the current checkout and refuses stale processes.

const path = require("node:path");
const fs = require("node:fs");
const { execFileSync } = require("node:child_process");

function readPackageVersion(repoRoot) {
  try {
    const raw = fs.readFileSync(path.join(repoRoot, "webapp", "package.json"), "utf8");
    const pkg = JSON.parse(raw);
    return typeof pkg.version === "string" ? pkg.version : "";
  } catch {
    return "";
  }
}

function readCheckoutSha(repoRoot) {
  try {
    return execFileSync("git", ["-C", repoRoot, "rev-parse", "HEAD"], {
      encoding: "utf8",
      timeout: 5000,
      windowsHide: true,
    }).trim();
  } catch {
    return "";
  }
}

/**
 * Identity snapshot for the checkout Electron will (or did) run the backend from.
 */
function currentBackendIdentity({
  repoRoot,
  appVersion = "",
  readSha = readCheckoutSha,
  readVersion = readPackageVersion,
} = {}) {
  const root = repoRoot ? path.resolve(repoRoot) : "";
  return {
    repoRoot: root,
    checkoutSha: root ? String(readSha(root) || "").trim() : "",
    packageVersion: root ? String(readVersion(root) || "").trim() : "",
    appVersion: String(appVersion || "").trim(),
  };
}

/**
 * Parse a backend.json marker into a normalized identity+port object, or null.
 */
function parseBackendMarker(raw) {
  if (!raw) return null;
  let m;
  try {
    m = typeof raw === "string" ? JSON.parse(raw) : raw;
  } catch {
    return null;
  }
  if (!m || typeof m !== "object") return null;
  const port = Number(m.port);
  if (!Number.isFinite(port) || port <= 0) return null;
  return {
    port,
    pid: m.pid != null ? Number(m.pid) : null,
    at: m.at != null ? Number(m.at) : null,
    repoRoot: typeof m.repoRoot === "string" ? m.repoRoot : "",
    checkoutSha: typeof m.checkoutSha === "string" ? m.checkoutSha : "",
    packageVersion: typeof m.packageVersion === "string" ? m.packageVersion : "",
    appVersion: typeof m.appVersion === "string" ? m.appVersion : "",
  };
}

/**
 * True when a candidate marker's identity matches what we would spawn now.
 * Markers missing identity fields are treated as mismatched (post-update safety:
 * an old marker from before this handshake must not be adopted).
 */
function markerMatchesIdentity(marker, expected) {
  if (!marker || !expected) return false;
  if (!marker.checkoutSha || !expected.checkoutSha) return false;
  if (marker.checkoutSha !== expected.checkoutSha) return false;
  if (marker.repoRoot && expected.repoRoot) {
    if (path.resolve(marker.repoRoot) !== path.resolve(expected.repoRoot)) return false;
  }
  if (marker.packageVersion && expected.packageVersion) {
    if (marker.packageVersion !== expected.packageVersion) return false;
  }
  return true;
}

/**
 * Decide whether to reuse a live authenticated backend or replace it.
 *
 * @returns {{ action: "reuse"|"replace"|"spawn", reason: string, marker: object|null }}
 */
function decideBackendReuse({ markerRaw, expectedIdentity, authenticated = false } = {}) {
  const marker = parseBackendMarker(markerRaw);
  if (!marker) {
    return { action: "spawn", reason: "no_marker", marker: null };
  }
  if (!authenticated) {
    return { action: "replace", reason: "not_authenticated", marker };
  }
  if (!markerMatchesIdentity(marker, expectedIdentity)) {
    return { action: "replace", reason: "identity_mismatch", marker };
  }
  return { action: "reuse", reason: "identity_match", marker };
}

/**
 * Build the JSON payload written to backend.json after a successful spawn.
 */
function buildBackendMarkerPayload({ port, pid, identity, at = Date.now() }) {
  return {
    port,
    pid,
    at,
    repoRoot: identity && identity.repoRoot ? identity.repoRoot : "",
    checkoutSha: identity && identity.checkoutSha ? identity.checkoutSha : "",
    packageVersion: identity && identity.packageVersion ? identity.packageVersion : "",
    appVersion: identity && identity.appVersion ? identity.appVersion : "",
  };
}

/**
 * Compare a live /api/config (or probe) identity payload against expected.
 * Used when the marker lacks fields but the running process can self-report.
 */
function liveIdentityMatches(live, expected) {
  if (!live || !expected) return false;
  const liveSha = String(live.checkout_sha || live.checkoutSha || "").trim();
  const expectSha = String(expected.checkoutSha || "").trim();
  if (!liveSha || !expectSha) return false;
  if (liveSha !== expectSha) return false;
  const liveVer = String(live.package_version || live.packageVersion || "").trim();
  const expectVer = String(expected.packageVersion || "").trim();
  if (liveVer && expectVer && liveVer !== expectVer) return false;
  return true;
}

module.exports = {
  readPackageVersion,
  readCheckoutSha,
  currentBackendIdentity,
  parseBackendMarker,
  markerMatchesIdentity,
  decideBackendReuse,
  buildBackendMarkerPayload,
  liveIdentityMatches,
};
