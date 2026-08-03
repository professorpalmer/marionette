"use strict";

// Keep an ALREADY-INSTALLED packaged checkout's Puppetmaster at the pin its
// source tree asks for, at startup, before the backend spawns.
//
// The packaged shell updates out-of-band from the checkout: a signed installer
// replaces app.asar while ~/.marionette/marionette advances by git pull. Both
// paths can move the Puppetmaster pin in webapp/electron/update-pm.cjs, but
// neither reinstalls the venv -- ensurePackagedCheckout() returns immediately
// once isInstallComplete() is true. Without this check a user who took a shell
// update (or whose git update failed mid-deps) keeps spawning the backend with
// the Puppetmaster their FIRST install happened to get.
//
// The same two escape hatches as the updater apply (they live in update-pm.cjs's
// plan): a custom MARIONETTE_PUPPETMASTER_SPEC and an editable dev checkout are
// never clobbered.
//
// Offline safety: this never throws and never blocks launch on a reachable
// PyPI. When the upgrade cannot run, it returns an explicit `stale` result for
// the caller to surface -- silently claiming the runtime is current is the one
// outcome that is not allowed.

const fs = require("node:fs");
const { execFile } = require("node:child_process");

const { venvPython } = require("./bootstrap.cjs");
const {
  installedPuppetmasterVersion,
  pinnedVersionFromSpec,
  resolveCheckoutPin,
} = require("./update-pm.cjs");

const PROBE_TIMEOUT_MS = 15_000;
const UPGRADE_TIMEOUT_MS = 180_000;

/** One-shot capture that resolves (never rejects) so startup can't be broken. */
function runCapture(cmd, args, { env, timeoutMs = PROBE_TIMEOUT_MS } = {}) {
  return new Promise((resolve) => {
    execFile(
      cmd,
      args,
      {
        env: env || process.env,
        timeout: timeoutMs,
        encoding: "utf8",
        maxBuffer: 10_000_000,
        windowsHide: true,
      },
      (err, stdout, stderr) => {
        resolve({
          ok: !err,
          out: String(stdout || "").trim(),
          err: String(stderr || (err && err.message) || "").trim(),
        });
      },
    );
  });
}

/**
 * Bring the checkout venv's Puppetmaster up to the checkout's pin.
 *
 * Every child process runs through `run` so tests drive this without uv, a
 * venv, or a network. Resolves to one of:
 *   { status: "current",  version, want }            -- venv already at the pin
 *   { status: "upgraded", from, to, spec }           -- venv was moved to the pin
 *   { status: "skipped",  reason, have, want }       -- escape hatch owns the install
 *   { status: "stale",    reason, have, want, spec } -- pin NOT met; surface it
 */
async function ensurePuppetmasterRuntime({
  repoRoot,
  env = process.env,
  python,
  run = runCapture,
  resolvePin = resolveCheckoutPin,
  exists = fs.existsSync,
  onProgress = () => {},
} = {}) {
  if (!repoRoot) return { status: "skipped", reason: "no checkout" };
  try {
    const interpreter = python || venvPython(repoRoot);
    if (!exists(interpreter)) {
      return { status: "stale", reason: `no venv interpreter at ${interpreter}` };
    }

    const pin = resolvePin(repoRoot);
    const want = pinnedVersionFromSpec(pin.pinnedSpec);
    // Marionette venvs come from `uv venv`, which omits pip, so prefer uv and
    // fall back to `python -m pip` for an older pip-bearing venv.
    const hasUv = (await run("uv", ["--version"], { env, timeoutMs: 5_000 })).ok;
    const show = hasUv
      ? await run("uv", ["pip", "show", "--python", interpreter, pin.distName], { env })
      : await run(interpreter, ["-m", "pip", "show", pin.distName], { env });
    const have = installedPuppetmasterVersion(show.out);

    const plan = pin.planPuppetmasterUpgrade({
      specEnv: env.MARIONETTE_PUPPETMASTER_SPEC,
      pipShowOutput: show.out,
      pinnedSpec: pin.pinnedSpec,
    });
    if (plan.skip) {
      if (want && have === want) return { status: "current", version: have, want };
      return { status: "skipped", reason: plan.reason, have, want };
    }

    // The checkout's pin wins over plan.spec: when the checkout module could not
    // be loaded, plan came from this frozen copy and its spec is the shell's
    // build-time pin, which is exactly the staleness we are here to fix.
    const spec = pin.pinnedSpec || plan.spec;
    onProgress(`Updating Puppetmaster to ${want || spec}...`);
    const install = hasUv
      ? await run("uv", ["pip", "install", "--python", interpreter, "--upgrade", spec],
          { env, timeoutMs: UPGRADE_TIMEOUT_MS })
      : await run(interpreter, ["-m", "pip", "install", "--upgrade", spec, "--quiet"],
          { env, timeoutMs: UPGRADE_TIMEOUT_MS });
    if (!install.ok) {
      return {
        status: "stale",
        reason: install.err || "Puppetmaster upgrade failed",
        have,
        want,
        spec,
      };
    }
    return { status: "upgraded", from: have, to: want, spec };
  } catch (e) {
    return { status: "stale", reason: String(e && e.message ? e.message : e) };
  }
}

/** True when the backend would start against a Puppetmaster older than the pin. */
function isRuntimeStale(result) {
  return !!(result && result.status === "stale");
}

/** One-line, user-facing summary for the update log and the update UI. */
function describeRuntimeParity(result) {
  if (!result) return "";
  switch (result.status) {
    case "current":
      return `Puppetmaster ${result.version} matches the checkout pin.`;
    case "upgraded":
      return `Puppetmaster upgraded ${result.from || "?"} -> ${result.to || "?"}.`;
    case "skipped":
      return `Puppetmaster left as-is: ${result.reason || "no checkout"}.`;
    case "stale":
      return (
        `Puppetmaster is ${result.have ? `at ${result.have}` : "unavailable"} but this ` +
        `Marionette needs ${result.want || "its pinned release"} -- ` +
        `${result.reason || "the upgrade could not run"}. Reconnect and update to finish.`
      );
    default:
      return "";
  }
}

/**
 * Fields an update-check payload carries so the UI can report a behind runtime.
 * Informational only: a stale runtime does not flip `available`, because the
 * usual cause is an offline machine and a permanent "update ready" nag the user
 * cannot clear is worse than the note.
 */
function runtimeParityFields(result) {
  if (!isRuntimeStale(result)) return {};
  return {
    runtimeStale: true,
    runtimeNote: describeRuntimeParity(result),
    runtimeHave: result.have || "",
    runtimeWant: result.want || "",
  };
}

module.exports = {
  PROBE_TIMEOUT_MS,
  UPGRADE_TIMEOUT_MS,
  runCapture,
  ensurePuppetmasterRuntime,
  isRuntimeStale,
  describeRuntimeParity,
  runtimeParityFields,
};
