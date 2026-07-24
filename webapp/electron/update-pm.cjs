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

const DEFAULT_PUPPETMASTER_SPEC = "puppetmaster-ai==1.21.0";

// True when `pip show` / `uv pip show` output describes an editable install
// (a dev checkout linked with `-e`), which we must not overwrite from PyPI.
function isEditableInstall(pipShowOutput) {
  return /^Editable project location:\s*\S/m.test(String(pipShowOutput || ""));
}

// Decide whether the updater should upgrade Puppetmaster, given the environment
// and the current install's `pip show` text. Returns either
//   { skip: true, reason }                       -- leave the install untouched
//   { skip: false, spec: "puppetmaster-ai==1.21.0" }     -- install the pinned PyPI release
function planPuppetmasterUpgrade({ specEnv, pipShowOutput } = {}) {
  const spec = String(specEnv || "").trim();
  if (spec) {
    return { skip: true, reason: "MARIONETTE_PUPPETMASTER_SPEC pins a custom spec" };
  }
  if (isEditableInstall(pipShowOutput)) {
    return { skip: true, reason: "editable install (dev checkout)" };
  }
  return { skip: false, spec: DEFAULT_PUPPETMASTER_SPEC };
}

module.exports = { DEFAULT_PUPPETMASTER_SPEC, isEditableInstall, planPuppetmasterUpgrade };
