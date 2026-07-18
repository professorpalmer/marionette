"use strict";

/**
 * Pure helpers for backend ownership / restart / shutdown semantics in Electron
 * main. Kept separate from main.cjs so node:test can pin the contract without
 * launching Electron.
 */

/** Marker filename written by POST /api/restart before self-terminate. */
const INTENTIONAL_RESTART_SIGNAL = "backend-restart.json";

/** Grace window after soft taskkill before force-kill on Windows. */
const WINDOWS_SHUTDOWN_GRACE_MS = 800;

/** True when this process may remove backend.json (it spawned the backend). */
function shouldUnlinkBackendMarker(backendOwned) {
  return backendOwned === true;
}

/**
 * Classify a backend child exit for logging + respawn policy.
 * - ignore: adopted, intentional Electron teardown, or quit in progress
 * - intentional_restart: backend wrote a restart signal (POST /api/restart)
 * - unexpected: owned crash — respawn + crash-loop accounting
 */
function classifyBackendExit({
  backendRef,
  backendOwned,
  quitting,
  restarting,
  intentionalRestart,
}) {
  if (!backendRef) return "ignore";
  if (quitting || restarting) return "ignore";
  if (backendOwned !== true) return "ignore";
  if (intentionalRestart) return "intentional_restart";
  return "unexpected";
}

/**
 * True when a backend child exit should trigger auto-respawn.
 * Intentional Electron teardown nulls `backend` before exit; adopted backends
 * are never owned and must not fight another instance's marker.
 */
function shouldRespawnAfterBackendExit(args) {
  const kind = classifyBackendExit(args);
  return kind === "unexpected" || kind === "intentional_restart";
}

/**
 * Parse + validate a restart-signal JSON blob. Fresh signals (within maxAgeMs)
 * are treated as intentional; stale/corrupt signals are ignored.
 */
function isFreshIntentionalRestartSignal(raw, { nowMs = Date.now(), maxAgeMs = 30_000 } = {}) {
  if (!raw) return false;
  let parsed;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return false;
  }
  if (!parsed || typeof parsed !== "object") return false;
  const at = Number(parsed.at);
  if (!Number.isFinite(at) || at <= 0) return false;
  return nowMs - at <= maxAgeMs;
}

/** Crash-loop accounting applies only to true unexpected exits. */
function shouldCountTowardCrashLoop(exitKind) {
  return exitKind === "unexpected";
}

/**
 * Ownership-safe Windows shutdown plan: soft tree signal, bounded grace, then
 * force. Callers MUST await the grace window before spawning a replacement so
 * SQLite/port locks from the old owned tree can release.
 */
function windowsBackendShutdownPlan({ graceMs = WINDOWS_SHUTDOWN_GRACE_MS } = {}) {
  return {
    softArgs: (pid) => ["/pid", String(pid), "/T"],
    forceArgs: (pid) => ["/pid", String(pid), "/T", "/F"],
    graceMs,
  };
}

function childProcessStillAlive(child) {
  if (!child) return false;
  if (child.exitCode != null) return false;
  if (child.signalCode != null) return false;
  if (child.killed) return false;
  return true;
}

/**
 * Shut down an owned backend process tree with a bounded graceful opportunity.
 * On Windows: soft taskkill /T, await graceMs while the child can exit, then
 * force /T /F only if still alive. Does not target unrelated processes.
 */
async function shutdownOwnedBackendTree({
  platform,
  child,
  spawnSync,
  sleep,
  graceMs = WINDOWS_SHUTDOWN_GRACE_MS,
  now = () => Date.now(),
}) {
  if (!child || !child.pid) {
    return { forced: false, softSignaled: false };
  }
  const pid = child.pid;

  if (platform === "win32") {
    const plan = windowsBackendShutdownPlan({ graceMs });
    let softSignaled = false;
    try {
      spawnSync("taskkill", plan.softArgs(pid), {
        windowsHide: true,
        timeout: 3000,
      });
      softSignaled = true;
    } catch {
      /* best-effort */
    }

    const deadline = now() + plan.graceMs;
    while (childProcessStillAlive(child) && now() < deadline) {
      const remaining = deadline - now();
      if (remaining <= 0) break;
      await sleep(Math.min(50, remaining));
    }

    if (!childProcessStillAlive(child)) {
      return { forced: false, softSignaled };
    }

    try {
      spawnSync("taskkill", plan.forceArgs(pid), {
        windowsHide: true,
        timeout: 5000,
      });
    } catch {
      /* best-effort */
    }
    try {
      child.kill();
    } catch {
      /* already gone */
    }
    return { forced: true, softSignaled };
  }

  // POSIX: detached process group — negative pid signals the whole tree.
  try {
    process.kill(-pid, "SIGTERM");
  } catch {
    try {
      child.kill("SIGTERM");
    } catch {
      /* ignore */
    }
  }

  const deadline = now() + graceMs;
  while (childProcessStillAlive(child) && now() < deadline) {
    const remaining = deadline - now();
    if (remaining <= 0) break;
    await sleep(Math.min(50, remaining));
  }

  if (!childProcessStillAlive(child)) {
    return { forced: false, softSignaled: true };
  }

  try {
    process.kill(-pid, "SIGKILL");
  } catch {
    try {
      if (!child.killed) child.kill("SIGKILL");
    } catch {
      /* ignore */
    }
  }
  return { forced: true, softSignaled: true };
}

module.exports = {
  INTENTIONAL_RESTART_SIGNAL,
  WINDOWS_SHUTDOWN_GRACE_MS,
  shouldUnlinkBackendMarker,
  classifyBackendExit,
  shouldRespawnAfterBackendExit,
  isFreshIntentionalRestartSignal,
  shouldCountTowardCrashLoop,
  windowsBackendShutdownPlan,
  childProcessStillAlive,
  shutdownOwnedBackendTree,
};
