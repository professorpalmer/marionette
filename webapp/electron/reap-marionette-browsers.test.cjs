"use strict";

const { test } = require("node:test");
const assert = require("node:assert/strict");
const {
  cmdlineIsMarionetteBrowser,
  parsePosixPs,
  parseWmicList,
  profileOwnership,
  reapMarionetteBrowsers,
  userDataDir,
} = require("./reap-marionette-browsers.cjs");

const CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const MAC = { home: "/Users/t", platform: "darwin" };

test("matcher accepts this home's profiles and rejects live Chrome", () => {
  for (const dir of [
    "/Users/t/.pmharness/browser-profile",
    "/Users/t/.pmharness/browser-profile-real/chrome",
    "/Users/t/.puppetmaster/browser-profiles/abcd",
    "/Users/T/.PMHARNESS/browser-profile",
  ]) {
    assert.equal(cmdlineIsMarionetteBrowser(`${CHROME} --user-data-dir=${dir} --no-first-run`, MAC), true, dir);
  }
  assert.equal(
    cmdlineIsMarionetteBrowser(`${CHROME} --user-data-dir=/Users/t/Library/Application Support/Google/Chrome`, MAC),
    false
  );
  assert.equal(cmdlineIsMarionetteBrowser(`${CHROME} --remote-debugging-port=9333`, MAC), false);
  assert.equal(cmdlineIsMarionetteBrowser("python -m harness.cli gui --port 1", MAC), false);
});

test("a sibling install under another HOME is not ours", () => {
  for (const dir of [
    "/Users/t/.pse-harness/home/.pmharness/browser-profile",
    "/tmp/fakehome/.pmharness/browser-profile",
    "/Users/t/.pmharness/browser-profile-other",
  ]) {
    assert.equal(profileOwnership(`${CHROME} --user-data-dir=${dir} --no-first-run`, MAC), null, dir);
  }
});

test("user-data-dir keeps spaces and ends at the next flag", () => {
  assert.equal(
    userDataDir(`${CHROME} --user-data-dir=/Users/t/My Stuff/.pmharness/browser-profile --remote-debugging-port=9`),
    "/Users/t/My Stuff/.pmharness/browser-profile"
  );
  assert.equal(
    profileOwnership(`${CHROME} --user-data-dir=/Users/t My/.pmharness/browser-profile`, {
      home: "/Users/t My",
      platform: "darwin",
    }),
    "home"
  );
});

test("Windows paths compare without case and slash direction", () => {
  const win = { home: "C:\\Users\\T", platform: "win32" };
  assert.equal(
    profileOwnership('chrome.exe --user-data-dir="c:\\users\\t\\.pmharness\\browser-profile" --no-first-run', win),
    "home"
  );
  assert.equal(
    profileOwnership('chrome.exe --user-data-dir="C:\\Users\\other\\.pmharness\\browser-profile"', win),
    null
  );
});

function withKill(fn) {
  const killed = [];
  const orig = process.kill;
  process.kill = (pid, sig) => {
    killed.push([pid, sig]);
  };
  try {
    return { result: fn(), killed };
  } finally {
    process.kill = orig;
  }
}

test("reap signals own and orphaned profiles, never a sibling's live ones", () => {
  const ps = [
    `  11  1 ${CHROME} --user-data-dir=/Users/t/Library/Application Support/Google/Chrome`,
    `  22  1 ${CHROME} --user-data-dir=/Users/t/.pmharness/browser-profile --no-first-run`,
    `  33  1 ${CHROME} --user-data-dir=/Users/t/.pse-harness/home/.pmharness/browser-profile --no-first-run`,
    "  40  1 /Applications/Marionette.app/Contents/MacOS/Marionette",
    "  41 40 python -m harness.cli gui --port 5",
    `  42 41 ${CHROME} --headless=new --user-data-dir=/var/folders/x/T/pm-cdp-sib --no-first-run`,
    "  50  1 python -m harness.cli gui --port 6",
    `  51 50 ${CHROME} --headless=new --user-data-dir=/var/folders/x/T/pm-cdp-mine --no-first-run`,
    `  61  1 ${CHROME} --headless=new --user-data-dir=/var/folders/x/T/pm-cdp-orphan --no-first-run`,
    `  71 999 ${CHROME} --headless=new --user-data-dir=/var/folders/x/T/pm-cdp-deadparent --no-first-run`,
  ].join("\n");
  const spawnSync = (cmd) => ({ stdout: cmd === "ps" ? ps : "" });
  const { result, killed } = withKill(() =>
    reapMarionetteBrowsers({ spawnSync, ...MAC, selfPid: 50 })
  );
  assert.deepEqual(result.signaled, [22, 51, 61, 71]);
  assert.deepEqual(killed.map(([pid]) => pid), [22, 51, 61, 71]);
});

test("Windows reap reads parent ids from wmic", () => {
  const calls = [];
  const wmic = [
    'CommandLine=chrome.exe --user-data-dir="C:\\Users\\t\\.pmharness\\browser-profile" --no-first-run',
    "ParentProcessId=4",
    "ProcessId=22",
    "",
    "CommandLine=chrome.exe --user-data-dir=C:\\Users\\t\\AppData\\Local\\Google\\Chrome\\User Data",
    "ParentProcessId=4",
    "ProcessId=11",
    "",
    "CommandLine=python -m harness.cli gui",
    "ParentProcessId=1",
    "ProcessId=4",
    "",
  ].join("\r\n");
  const spawnSync = (cmd, args) => {
    calls.push([cmd, args]);
    return { stdout: cmd === "wmic" ? wmic : "" };
  };
  const result = reapMarionetteBrowsers({ spawnSync, platform: "win32", home: "C:\\Users\\t", selfPid: 9 });
  assert.deepEqual(result.signaled, [22]);
  assert.deepEqual(calls[1], ["taskkill", ["/PID", "22", "/T", "/F"]]);
});

test("parsers skip junk lines", () => {
  assert.deepEqual(parsePosixPs("  9 1 chrome --user-data-dir=/x\nbad\n"), [
    { pid: 9, ppid: 1, cmdline: "chrome --user-data-dir=/x" },
  ]);
  assert.deepEqual(parseWmicList("junk\r\n\r\nProcessId=0\r\n"), []);
});
