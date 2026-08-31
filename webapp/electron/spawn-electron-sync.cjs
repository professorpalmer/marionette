"use strict";

const { spawnSync } = require("node:child_process");

const BUSY = new Set(["ETXTBSY", "EAGAIN"]);

function waitMs(ms) {
  spawnSync(
    process.execPath,
    ["-e", `Atomics.wait(new Int32Array(new SharedArrayBuffer(4)),0,0,${ms})`],
    { timeout: ms + 500 },
  );
}

/** spawnSync Electron, retrying ETXTBSY (parallel first-launch on Linux CI). */
function spawnElectronSync(bin, args, opts, attempts, spawn) {
  const run = spawn || spawnSync;
  const max = attempts == null ? 6 : attempts;
  let result;
  for (let i = 0; i < max; i++) {
    result = run(bin, args, opts);
    const code = result && result.error && result.error.code;
    if (!BUSY.has(code) || i === max - 1) return result;
    waitMs(40 * (i + 1));
  }
  return result;
}

module.exports = { spawnElectronSync, BUSY };
