"use strict";

// Retry-once policy for the renderer rebuild during self-update.
//
// Adapted from the Hermes Agent desktop updater (update-rebuild.cjs, MIT, Nous
// Research).
//
// The first rebuild can return nonzero on a still-settling post-pull tree (a
// half-written file, a transient resolver hiccup). A second attempt then builds
// clean off the settled source. Without the retry the updater would bail before
// the relaunch step -- the source updated but the app never restarted.

function shouldRetryRebuild(code) {
  return code !== 0;
}

// Run `rebuild(attempt)` (async, resolves `{ code, ... }`), retrying once on
// failure. Returns the final result.
async function runRebuildWithRetry(rebuild) {
  let result = await rebuild(0);
  if (shouldRetryRebuild(result.code)) {
    result = await rebuild(1);
  }
  return result;
}

module.exports = { shouldRetryRebuild, runRebuildWithRetry };
