"use strict";

// Keep Puppetmaster -- Marionette's one integral runtime dependency -- current
// during self-update.
//
// Puppetmaster ships out-of-band from this repo: it is a pinned PyPI
// package (`puppetmaster-ai`, imported as `puppetmaster`), installed alongside
// Marionette by the installer (scripts/install.sh). Because it is not part of
// Marionette's git history, a `git pull` of the app never carries a Puppetmaster
// release. Without this step an existing install would stay frozen on whatever
// Puppetmaster it happened to get at first-install time, even as the app
// self-updates -- so PM overhauls would only ever reach *new* installs.
//
// The apply pipeline therefore upgrades Puppetmaster on every update. Two
// escape hatches keep dev/CI checkouts intact:
//   - MARIONETTE_PUPPETMASTER_SPEC set  -> a contributor pinned a custom spec
//     (the same knob install.sh honors, often a local path); leave it alone.
//   - an editable install               -> `pip show` reports an "Editable
//     project location"; that is a dev checkout managing its own source, so we
//     never clobber it with a PyPI wheel.

const path = require("node:path");

const DEFAULT_PUPPETMASTER_SPEC = "puppetmaster-ai==1.22.39";
const PUPPETMASTER_DIST_NAME = "puppetmaster-ai";
const HARNESS_DIST_NAME = "pm-harness";

// True when `pip show` / `uv pip show` output describes an editable install
// (a dev checkout linked with `-e`), which we must not overwrite from PyPI.
function isEditableInstall(pipShowOutput) {
  return /^Editable project location:\s*\S/m.test(String(pipShowOutput || ""));
}

// Path from `pip show` / `uv pip show` for an editable install (`-e` checkout).
function parseEditableProjectLocation(pipShowOutput) {
  const m = String(pipShowOutput || "").match(/^Editable project location:\s*(.+)$/m);
  return m ? m[1].trim() : "";
}

function staleHarnessEditableNotice(editablePath) {
  return (
    `Harness was an editable install at ${editablePath}; brought it to the pulled tree ` +
    "so the server cannot keep serving a stale checkout."
  );
}

// Decide how to reconcile a venv whose editable pm-harness points at a different
// checkout than the tree we just pulled. Returns { skip: true } when aligned or
// not editable; otherwise { skip: false, action: "ff"|"rebind", notice, ... }.
function planStaleHarnessEditable({
  repoRootRealpath,
  editableLocationRealpath,
  pipShowOutput,
  sameRemote,
  editableCanFf,
} = {}) {
  const editableLocation = parseEditableProjectLocation(pipShowOutput);
  if (!isEditableInstall(pipShowOutput) || !editableLocation) {
    return { skip: true, reason: "not an editable harness install" };
  }
  const editPath = String(editableLocationRealpath || editableLocation);
  const repoPath = String(repoRootRealpath || "");
  if (repoPath && editPath && repoPath === editPath) {
    return { skip: true, reason: "editable already aligned with repo root" };
  }
  const notice = staleHarnessEditableNotice(editableLocation);
  if (sameRemote && editableCanFf) {
    return {
      skip: false,
      action: "ff",
      notice,
      editableLocation,
      editableLocationRealpath: editPath,
    };
  }
  return {
    skip: false,
    action: "rebind",
    notice,
    editableLocation,
    editableLocationRealpath: editPath,
  };
}

function pinnedVersionFromSpec(spec) {
  const m = String(spec || "").match(/==\s*([^\s]+)/);
  return m ? m[1] : "";
}

function installedPuppetmasterVersion(pipShowOutput) {
  const m = String(pipShowOutput || "").match(/^Version:\s*(\S+)/m);
  return m ? m[1] : "";
}

// Decide whether the updater should upgrade Puppetmaster, given the environment
// and the current install's `pip show` text. Returns either
//   { skip: true, reason }                       -- leave the install untouched
//   { skip: false, spec: "puppetmaster-ai==1.22.39" }    -- install the pinned PyPI release
function planPuppetmasterUpgrade({ specEnv, pipShowOutput, pinnedSpec } = {}) {
  const spec = String(specEnv || "").trim();
  if (spec) {
    return { skip: true, reason: "MARIONETTE_PUPPETMASTER_SPEC pins a custom spec" };
  }
  if (isEditableInstall(pipShowOutput)) {
    return { skip: true, reason: "editable install (dev checkout)" };
  }
  const want = pinnedVersionFromSpec(pinnedSpec || DEFAULT_PUPPETMASTER_SPEC);
  const have = installedPuppetmasterVersion(pipShowOutput);
  if (want && have && have === want) {
    return { skip: true, reason: `already at ${want}` };
  }
  return { skip: false, spec: DEFAULT_PUPPETMASTER_SPEC, have: have || "", want };
}

/**
 * Resolve the Puppetmaster pin from the CHECKOUT's update-pm.cjs when present.
 * A packaged shell's own require("./update-pm.cjs") is frozen in app.asar; the
 * checkout's copy moves with `git pull`, so it is authoritative (otherwise PM
 * stays stuck on the shell's build-time pin, e.g. 1.21.6 after the tree moved
 * to 1.22.39).
 *
 * @returns {{ pinnedSpec: string, distName: string, planPuppetmasterUpgrade: Function }}
 */
function resolveCheckoutPin(repoRoot) {
  try {
    const checkoutPinPath = path.join(repoRoot, "webapp", "electron", "update-pm.cjs");
    // Clear cache so a just-pulled pin is visible without relaunching Electron.
    try { delete require.cache[require.resolve(checkoutPinPath)]; } catch { /* first load */ }
    const checkoutPm = require(checkoutPinPath);
    if (checkoutPm && checkoutPm.DEFAULT_PUPPETMASTER_SPEC) {
      return {
        pinnedSpec: checkoutPm.DEFAULT_PUPPETMASTER_SPEC,
        distName: checkoutPm.PUPPETMASTER_DIST_NAME || PUPPETMASTER_DIST_NAME,
        planPuppetmasterUpgrade: checkoutPm.planPuppetmasterUpgrade || planPuppetmasterUpgrade,
      };
    }
  } catch { /* fall through to this module's (packaged) pin */ }
  return {
    pinnedSpec: DEFAULT_PUPPETMASTER_SPEC,
    distName: PUPPETMASTER_DIST_NAME,
    planPuppetmasterUpgrade,
  };
}

module.exports = {
  DEFAULT_PUPPETMASTER_SPEC,
  PUPPETMASTER_DIST_NAME,
  HARNESS_DIST_NAME,
  isEditableInstall,
  parseEditableProjectLocation,
  staleHarnessEditableNotice,
  planStaleHarnessEditable,
  pinnedVersionFromSpec,
  installedPuppetmasterVersion,
  planPuppetmasterUpgrade,
  resolveCheckoutPin,
};
