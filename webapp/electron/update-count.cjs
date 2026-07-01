"use strict";

// Pure helpers for resolving how far the local checkout is behind its branch.
//
// Adapted from the Hermes Agent desktop updater (update-count.cjs, MIT, Nous
// Research).
//
// Whether `git rev-list HEAD..origin/<branch> --count` produces a meaningful
// number worth trusting. On a SHALLOW checkout (a bootstrap clone with
// --depth 1) the local history often shares no merge-base with the freshly
// fetched origin tip, so the count enumerates the entire remote ancestry and
// returns a bogus huge number. In that case fall back to a binary SHA compare.
function shouldCountCommits({ isShallow, hasMergeBase }) {
  return !(isShallow && !hasMergeBase);
}

// Resolve how many commits the local checkout is behind origin for the update
// indicator. When the exact count isn't meaningful (shallow + no merge-base)
// fall back to a binary up-to-date check by SHA: equal => 0 (up to date),
// otherwise 1 (behind by an unknown amount -> generic "update available").
function resolveBehindCount({ countStr, currentSha, targetSha, isShallow, hasMergeBase }) {
  if (!shouldCountCommits({ isShallow, hasMergeBase })) {
    if (currentSha && targetSha && currentSha === targetSha) return 0;
    return 1;
  }
  return Number.parseInt(countStr, 10) || 0;
}

module.exports = { resolveBehindCount, shouldCountCommits };
