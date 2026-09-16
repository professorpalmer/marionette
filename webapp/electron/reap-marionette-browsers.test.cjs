"use strict";

const { test } = require("node:test");
const assert = require("node:assert/strict");
const {
  cmdlineIsMarionetteBrowser,
  parsePosixPs,
  reapMarionetteBrowsers,
} = require("./reap-marionette-browsers.cjs");

test("matcher accepts Marionette profiles and rejects live Chrome", () => {
  const chrome = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
  assert.equal(
    cmdlineIsMarionetteBrowser(
      `${chrome} --user-data-dir=/Users/t/.pmharness/browser-profile --remote-debugging-port=9333`
    ),
    true
  );
  assert.equal(
    cmdlineIsMarionetteBrowser(
      `${chrome} --user-data-dir=/Users/t/Library/Application Support/Google/Chrome`
    ),
    false
  );
  assert.equal(
    cmdlineIsMarionetteBrowser(`${chrome} --remote-debugging-port=9333`),
    false
  );
});

test("reap signals only matching pids", () => {
  const killed = [];
  const spawnSync = (cmd, args) => {
    if (cmd === "ps") {
      return {
        stdout: [
          "  11 /Applications/Google Chrome.app/Contents/MacOS/Google Chrome --user-data-dir=/Users/t/Library/Application Support/Google/Chrome",
          "  22 /Applications/Google Chrome.app/Contents/MacOS/Google Chrome --user-data-dir=/Users/t/.pmharness/browser-profile",
          "  33 python -m harness.cli gui",
        ].join("\n"),
      };
    }
    return { stdout: "" };
  };
  const orig = process.kill;
  process.kill = (pid, sig) => {
    killed.push([pid, sig]);
  };
  try {
    const result = reapMarionetteBrowsers({ spawnSync, platform: "darwin" });
    assert.deepEqual(result.signaled, [22]);
    assert.deepEqual(killed, [[22, "SIGTERM"]]);
  } finally {
    process.kill = orig;
  }
});

test("parsePosixPs skips junk lines", () => {
  assert.deepEqual(parsePosixPs("  9 chrome --user-data-dir=/x\nbad\n"), [
    { pid: 9, cmdline: "chrome --user-data-dir=/x" },
  ]);
});
