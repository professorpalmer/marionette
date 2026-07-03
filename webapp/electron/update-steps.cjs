"use strict";

// Pure description of the self-update apply pipeline: the ordered stages, their
// relative cost weights, and a mapping from (stage, within-stage ratio) to an
// overall 0..100 percent for the progress bar. Kept pure so the percent math is
// unit-testable without running git/npm.
//
// The apply flow (orchestrated in update-bridge.cjs) runs these against the
// checkout the app is launched from:
//   fetch  -> git fetch the branch tip
//   pull   -> fast-forward the working tree to origin/<branch>
//   deps   -> refresh Python + node deps if their lockfiles changed, and always
//             upgrade Puppetmaster (the integral runtime dep ships out-of-band)
//   build  -> rebuild the renderer (tsc -b && vite build) into dist/
//   relaunch is a terminal action, not a measured stage.

const STAGES = [
  { id: "fetch", label: "Fetching latest", weight: 1 },
  { id: "pull", label: "Updating source", weight: 1 },
  { id: "deps", label: "Refreshing dependencies", weight: 3 },
  { id: "build", label: "Rebuilding app", weight: 6 },
];

const TOTAL_WEIGHT = STAGES.reduce((sum, s) => sum + s.weight, 0);

// The cumulative percent band [start, end] each stage occupies.
function stageBands() {
  const bands = {};
  let acc = 0;
  for (const s of STAGES) {
    const start = (acc / TOTAL_WEIGHT) * 100;
    acc += s.weight;
    const end = (acc / TOTAL_WEIGHT) * 100;
    bands[s.id] = { start, end };
  }
  return bands;
}

// Overall 0..100 percent given the active stage and how far through it we are
// (ratio 0..1). Unknown stage -> null (indeterminate). Terminal states map to
// their natural endpoints.
function overallPercent(stageId, ratio = 0) {
  if (stageId === "idle") return 0;
  if (stageId === "done" || stageId === "relaunch") return 100;
  const bands = stageBands();
  const band = bands[stageId];
  if (!band) return null;
  const r = Math.max(0, Math.min(1, ratio));
  return Math.round(band.start + (band.end - band.start) * r);
}

function stageLabel(stageId) {
  const hit = STAGES.find((s) => s.id === stageId);
  return hit ? hit.label : "";
}

module.exports = { STAGES, TOTAL_WEIGHT, stageBands, overallPercent, stageLabel };
