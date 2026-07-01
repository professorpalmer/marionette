"use strict";

// In-app update mutual-exclusion marker.
//
// Adapted from the Hermes Agent desktop updater (update-marker.cjs, MIT, Nous
// Research).
//
// While an update is applying (git pull + deps + rebuild) we write
// ~/.pmharness/.pmharness-update-in-progress. The marker body is two lines: the
// applying process's pid and the unix-seconds it started.
//
// Why: if the user relaunches mid-update (the window vanished during the rebuild
// and looks crashed), a fresh instance must NOT spawn its own local backend on
// the same SQLite state and race the update. The main process gates backend
// startup on this marker and parks until the update finishes. A stale marker
// (dead pid, or older than the ceiling) self-heals: it is deleted on read.

const fs = require("node:fs");
const path = require("node:path");

// Even with a live-looking pid, never treat a marker older than this as a live
// update. A full update (git pull + pip + webapp rebuild) is a couple of
// minutes; past this the marker is almost certainly stale (e.g. the OS recycled
// the pid onto an unrelated process), so the gate self-heals.
const UPDATE_MARKER_MAX_AGE_MS = 20 * 60 * 1000;

function markerPath(pmharnessHome) {
  return path.join(pmharnessHome, ".pmharness-update-in-progress");
}

// True only if a host process with this pid is currently alive. Signal 0 does
// not deliver a signal -- it just probes existence/permission. ESRCH => dead;
// EPERM => alive but owned by another user (still "alive" for our purposes).
// Injectable `kill` keeps it unit-testable.
function isPidAlive(pid, kill = process.kill.bind(process)) {
  if (!Number.isInteger(pid) || pid <= 0) return false;
  try {
    kill(pid, 0);
    return true;
  } catch (err) {
    return Boolean(err && err.code === "EPERM");
  }
}

// Write the marker for the given pid at the current time.
function writeMarker(pmharnessHome, pid = process.pid, now = Date.now) {
  const file = markerPath(pmharnessHome);
  try {
    fs.mkdirSync(pmharnessHome, { recursive: true });
    fs.writeFileSync(file, `${pid}\n${Math.floor(now() / 1000)}\n`);
    return true;
  } catch {
    return false;
  }
}

// Remove the marker (best-effort). Called when an apply finishes or fails.
function clearMarker(pmharnessHome) {
  try {
    fs.unlinkSync(markerPath(pmharnessHome));
  } catch {
    void 0;
  }
}

// Read + interpret the marker.
//
// Returns `{ pid, ageMs }` only when an update is GENUINELY still running
// (parseable pid that is alive, within the age ceiling). Returns `null` for
// every "no live update" case -- absent, unreadable, malformed, dead pid, or
// past the ceiling -- and, when a stale marker file exists, deletes it so it
// cannot strand future launches.
function readLiveUpdateMarker(pmharnessHome, { kill, now = Date.now, maxAgeMs = UPDATE_MARKER_MAX_AGE_MS } = {}) {
  const file = markerPath(pmharnessHome);
  let raw;
  try {
    raw = fs.readFileSync(file, "utf8");
  } catch {
    return null;
  }

  const [pidLine, startedLine] = String(raw).split("\n");
  const pid = Number.parseInt((pidLine || "").trim(), 10);
  const startedAt = Number.parseInt((startedLine || "").trim(), 10);
  const ageMs = Number.isFinite(startedAt) ? now() - startedAt * 1000 : Infinity;
  const alive = Number.isInteger(pid) && isPidAlive(pid, kill);

  if (!alive || ageMs > maxAgeMs) {
    try {
      fs.unlinkSync(file);
    } catch {
      void 0;
    }
    return null;
  }
  return { pid, ageMs };
}

module.exports = {
  UPDATE_MARKER_MAX_AGE_MS,
  markerPath,
  isPidAlive,
  writeMarker,
  clearMarker,
  readLiveUpdateMarker,
};
