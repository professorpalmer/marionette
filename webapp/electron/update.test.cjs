"use strict";

// Unit tests for the pure self-update helpers. These run without booting
// Electron: `node --test electron/*.test.cjs` (see package.json `test:electron`).

const { test } = require("node:test");
const assert = require("node:assert/strict");
const os = require("node:os");
const fs = require("node:fs");
const path = require("node:path");

const remote = require("./update-remote.cjs");
const count = require("./update-count.cjs");
const steps = require("./update-steps.cjs");
const rebuild = require("./update-rebuild.cjs");
const pm = require("./update-pm.cjs");
const env = require("./update-env.cjs");
const marker = require("./update-marker.cjs");
const bridge = require("./update-bridge.cjs");

test("canonicalGitHubRemote: ssh and https forms of the same repo compare equal", () => {
  const ssh = remote.canonicalGitHubRemote("git@github.com:professorpalmer/marionette.git");
  const https = remote.canonicalGitHubRemote("https://github.com/professorpalmer/marionette.git");
  assert.equal(ssh, "github.com/professorpalmer/marionette");
  assert.equal(ssh, https);
});

test("chooseFetchRemote: official SSH remote -> public HTTPS (dodge passkey prompt)", () => {
  assert.equal(
    remote.chooseFetchRemote("git@github.com:professorpalmer/marionette.git"),
    remote.OFFICIAL_REPO_HTTPS_URL
  );
});

test("chooseFetchRemote: HTTPS origin and forks fetch from 'origin' unchanged", () => {
  assert.equal(remote.chooseFetchRemote("https://github.com/professorpalmer/marionette.git"), "origin");
  assert.equal(remote.chooseFetchRemote("git@github.com:someone/fork.git"), "origin");
});

test("resolveBehindCount: normal full clone uses the exact count", () => {
  assert.equal(
    count.resolveBehindCount({ countStr: "3", isShallow: false, hasMergeBase: true }),
    3
  );
});

test("resolveBehindCount: shallow + no merge-base falls back to SHA compare", () => {
  assert.equal(
    count.resolveBehindCount({ countStr: "12104", currentSha: "abc", targetSha: "abc", isShallow: true, hasMergeBase: false }),
    0
  );
  assert.equal(
    count.resolveBehindCount({ countStr: "12104", currentSha: "abc", targetSha: "def", isShallow: true, hasMergeBase: false }),
    1
  );
});

test("overallPercent: monotonic across the pipeline, clamped to 0..100", () => {
  assert.equal(steps.overallPercent("idle"), 0);
  const fetchEnd = steps.overallPercent("fetch", 1);
  const buildStart = steps.overallPercent("build", 0);
  assert.ok(fetchEnd <= buildStart, "fetch completes before build starts");
  assert.equal(steps.overallPercent("done"), 100);
  assert.equal(steps.overallPercent("build", 5), 100); // ratio clamped
  assert.equal(steps.overallPercent("bogus", 0.5), null);
});

test("runRebuildWithRetry: retries exactly once on failure then stops", async () => {
  let attempts = 0;
  const res = await rebuild.runRebuildWithRetry(async () => {
    attempts += 1;
    return { code: attempts === 1 ? 1 : 0 };
  });
  assert.equal(attempts, 2);
  assert.equal(res.code, 0);
});

test("runRebuildWithRetry: a first-try success does not retry", async () => {
  let attempts = 0;
  const res = await rebuild.runRebuildWithRetry(async () => {
    attempts += 1;
    return { code: 0 };
  });
  assert.equal(attempts, 1);
  assert.equal(res.code, 0);
});

test("planPuppetmasterUpgrade: a plain PyPI install upgrades to the pinned Puppetmaster release", () => {
  const plan = pm.planPuppetmasterUpgrade({
    specEnv: "",
    pipShowOutput: "Name: puppetmaster-ai\nVersion: 1.1.0\nLocation: /app/.venv/lib/python3.11/site-packages",
  });
  assert.equal(plan.skip, false);
  assert.equal(plan.spec, pm.DEFAULT_PUPPETMASTER_SPEC);
  assert.equal(plan.have, "1.1.0");
});

test("planPuppetmasterUpgrade: already at the pin is a no-op", () => {
  const want = pm.pinnedVersionFromSpec(pm.DEFAULT_PUPPETMASTER_SPEC);
  const plan = pm.planPuppetmasterUpgrade({
    specEnv: "",
    pipShowOutput: `Name: puppetmaster-ai\nVersion: ${want}\nLocation: /app/.venv/lib/python3.11/site-packages`,
  });
  assert.equal(plan.skip, true);
  assert.match(plan.reason, new RegExp(`already at ${want}`));
});

test("planPuppetmasterUpgrade: missing show output still upgrades (fresh / broken venv)", () => {
  const plan = pm.planPuppetmasterUpgrade({
    specEnv: "",
    pipShowOutput: "",
  });
  assert.equal(plan.skip, false);
  assert.equal(plan.spec, pm.DEFAULT_PUPPETMASTER_SPEC);
});

test("planPuppetmasterUpgrade: an editable dev checkout is left untouched", () => {
  const plan = pm.planPuppetmasterUpgrade({
    specEnv: "",
    pipShowOutput: "Name: puppetmaster-ai\nVersion: 1.1.0\nEditable project location: /Users/dev/Puppetmaster",
  });
  assert.equal(plan.skip, true);
  assert.match(plan.reason, /editable/);
});

test("operational Puppetmaster pins match DEFAULT_PUPPETMASTER_SPEC", () => {
  const spec = pm.DEFAULT_PUPPETMASTER_SPEC;
  assert.match(spec, /^puppetmaster-ai==\d+\.\d+\.\d+$/);
  const repoRoot = path.join(__dirname, "..", "..");
  const copies = [
    "webapp/electron/update-pm.cjs",
    "webapp/electron/bootstrap.cjs",
    "scripts/install.sh",
    "scripts/install.ps1",
    "scripts/doctor.sh",
    "scripts/doctor.ps1",
    "FINDINGS.md",
    "harness/diag_bundle.py",
    ".github/workflows/tests.yml",
    ".github/workflows/tests-full.yml",
    ".github/workflows/release.yml",
  ];
  const escaped = spec.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const specRe = new RegExp(escaped);
  for (const rel of copies) {
    const text = fs.readFileSync(path.join(repoRoot, rel), "utf8");
    assert.match(text, specRe, `${rel} must pin ${spec}`);
  }
});

test("planPuppetmasterUpgrade: a custom MARIONETTE_PUPPETMASTER_SPEC is honored (never clobbered)", () => {
  const plan = pm.planPuppetmasterUpgrade({
    specEnv: "/Users/dev/Puppetmaster",
    pipShowOutput: "Name: puppetmaster-ai\nVersion: 1.1.0",
  });
  assert.equal(plan.skip, true);
  assert.match(plan.reason, /MARIONETTE_PUPPETMASTER_SPEC/);
});

test("isEditableInstall: matches only the editable marker line", () => {
  assert.equal(pm.isEditableInstall("Editable project location: /x"), true);
  assert.equal(pm.isEditableInstall("Location: /x/site-packages"), false);
  assert.equal(pm.isEditableInstall(""), false);
});

test("parseEditableProjectLocation: extracts the editable checkout path from pip show", () => {
  const out = [
    "Name: pm-harness",
    "Version: 0.9.284",
    "Editable project location: /Users/dev/Projects/marionette",
    "Location: /Users/.marionette/marionette/.venv/lib/python3.11/site-packages",
  ].join("\n");
  assert.equal(pm.parseEditableProjectLocation(out), "/Users/dev/Projects/marionette");
  assert.equal(pm.parseEditableProjectLocation("Name: pm-harness\nVersion: 0.9.284"), "");
});

test("planStaleHarnessEditable: aligned editable location is a no-op", () => {
  const pip = "Name: pm-harness\nEditable project location: /app/marionette\n";
  const plan = pm.planStaleHarnessEditable({
    repoRootRealpath: "/app/marionette",
    editableLocationRealpath: "/app/marionette",
    pipShowOutput: pip,
    sameRemote: true,
    editableCanFf: true,
  });
  assert.equal(plan.skip, true);
  assert.match(plan.reason, /aligned/);
});

test("planStaleHarnessEditable: mismatched path + clean same-remote plans fast-forward", () => {
  const pip = "Name: pm-harness\nEditable project location: /Users/dev/Projects/marionette\n";
  const plan = pm.planStaleHarnessEditable({
    repoRootRealpath: "/Users/.marionette/marionette",
    editableLocationRealpath: "/Users/dev/Projects/marionette",
    pipShowOutput: pip,
    sameRemote: true,
    editableCanFf: true,
  });
  assert.equal(plan.skip, false);
  assert.equal(plan.action, "ff");
  assert.match(plan.notice, /Harness was an editable install at/);
  assert.match(plan.notice, /stale checkout/);
});

test("planStaleHarnessEditable: dirty or diverged editable checkout plans rebind", () => {
  const pip = "Name: pm-harness\nEditable project location: /Users/dev/Projects/marionette\n";
  const dirty = pm.planStaleHarnessEditable({
    repoRootRealpath: "/Users/.marionette/marionette",
    editableLocationRealpath: "/Users/dev/Projects/marionette",
    pipShowOutput: pip,
    sameRemote: true,
    editableCanFf: false,
  });
  assert.equal(dirty.skip, false);
  assert.equal(dirty.action, "rebind");

  const remote = pm.planStaleHarnessEditable({
    repoRootRealpath: "/Users/.marionette/marionette",
    editableLocationRealpath: "/Users/dev/fork/marionette",
    pipShowOutput: "Name: pm-harness\nEditable project location: /Users/dev/fork/marionette\n",
    sameRemote: false,
    editableCanFf: true,
  });
  assert.equal(remote.action, "rebind");
});

test("planStaleHarnessEditable: after rebind alignment the plan is a no-op", () => {
  const home = "/Users/.marionette/marionette";
  const pip = `Name: pm-harness\nEditable project location: ${home}\n`;
  const plan = pm.planStaleHarnessEditable({
    repoRootRealpath: home,
    editableLocationRealpath: home,
    pipShowOutput: pip,
    sameRemote: true,
    editableCanFf: true,
  });
  assert.equal(plan.skip, true);
});

test("staleHarnessEditableNotice: names the old editable path", () => {
  const notice = pm.staleHarnessEditableNotice("/Users/dev/Projects/marionette");
  assert.match(notice, /\/Users\/dev\/Projects\/marionette/);
  assert.match(notice, /cannot keep serving a stale checkout/);
});

test("buildUpdaterEnv: login-shell PATH is prepended so npm/uv resolve (fixes spawn ENOENT)", () => {
  // Join fixture PATHs with the platform delimiter -- hardcoded ":" would not
  // split on Windows and the assertions below would see one giant segment.
  const joinPath = (...dirs) => dirs.join(path.delimiter);
  const merged = env.buildUpdaterEnv({
    processEnv: { PATH: joinPath("/usr/bin", "/bin"), HARNESS_TOKEN: "keep-me" },
    shellEnv: { PATH: joinPath("/opt/homebrew/bin", "/usr/bin"), SSH_AUTH_SOCK: "/tmp/agent.sock" },
  });
  const parts = merged.PATH.split(path.delimiter);
  assert.equal(parts[0], "/opt/homebrew/bin", "homebrew (shell) dir comes first");
  assert.ok(parts.includes("/bin"), "base PATH dirs are preserved");
  assert.equal(parts.filter((p) => p === "/usr/bin").length, 1, "duplicate dirs are de-duplicated");
  assert.equal(merged.SSH_AUTH_SOCK, "/tmp/agent.sock", "shell-only vars are merged in");
  assert.equal(merged.HARNESS_TOKEN, "keep-me", "base env vars are preserved");
});

test("buildUpdaterEnv: an empty shell env leaves the base PATH intact", () => {
  const basePath = ["/usr/bin", "/bin"].join(path.delimiter);
  const merged = env.buildUpdaterEnv({ processEnv: { PATH: basePath }, shellEnv: {} });
  assert.equal(merged.PATH, basePath);
});

test("mergePathStrings: order-preserving de-duplication across segments", () => {
  // Use platform-neutral segments — path.delimiter is ";" on win32 and ":" on
  // Linux CI; hard-coded Windows paths made the assertion host-specific.
  const a = path.join("tools");
  const b = path.join("bin");
  const c = path.join("extra");
  const joinPath = (...dirs) => dirs.join(path.delimiter);
  const merged = env.mergePathStrings(joinPath(a, b), joinPath(b, c));
  const parts = merged.split(path.delimiter);
  assert.deepEqual(parts, [a, b, c]);
});

test("parseRegQueryPath: extracts PATH value from reg query output", () => {
  const sample = [
    "",
    "HKEY_CURRENT_USER\\Environment",
    "    PATH    REG_EXPAND_SZ    %USERPROFILE%\\bin;C:\\Windows",
    "",
  ].join("\r\n");
  assert.equal(
    env.parseRegQueryPath(sample),
    "%USERPROFILE%\\bin;C:\\Windows",
  );
});

test("expandWinEnv: expands %VAR% tokens against a supplied env map", () => {
  const expanded = env.expandWinEnv("%USERPROFILE%\\bin;%APPDATA%\\npm", {
    USERPROFILE: "C:\\Users\\dev",
    APPDATA: "C:\\Users\\dev\\AppData\\Roaming",
  });
  assert.equal(
    expanded,
    "C:\\Users\\dev\\bin;C:\\Users\\dev\\AppData\\Roaming\\npm",
  );
});

test("windowsProfilePathCandidates: includes npm, uv, and portable tool dirs", () => {
  // Match helper construction: path.join for USERPROFILE-relative dirs;
  // path.win32 for stock MSI literals (Windows-shaped even on Linux CI).
  const home = path.join("C:", "Users", "dev");
  const localAppData = path.join(home, "AppData", "Local");
  const appData = path.join(home, "AppData", "Roaming");
  const candidates = env.windowsProfilePathCandidates({
    USERPROFILE: home,
    LOCALAPPDATA: localAppData,
    APPDATA: appData,
  });
  assert.ok(candidates.includes(path.join(appData, "npm")));
  assert.ok(candidates.includes(path.join(home, ".local", "bin")));
  assert.ok(
    candidates.includes(path.join(localAppData, "marionette", "tools", "node")),
  );
  // Default MSI install dir even when NVM_SYMLINK is unset (candidates, not exists filter).
  assert.ok(candidates.includes(path.win32.join("C:", "Program Files", "nodejs")));
});

test("windowsShellEnv: merges profile, registry, and inherited PATH on win32", () => {
  if (process.platform !== "win32") return;
  const shellEnv = env.windowsShellEnv({
    USERPROFILE: process.env.USERPROFILE,
    LOCALAPPDATA: process.env.LOCALAPPDATA,
    APPDATA: process.env.APPDATA,
    PATH: process.env.PATH,
  });
  assert.ok(shellEnv.PATH, "expected a merged PATH on Windows");
  const parts = shellEnv.PATH.split(path.delimiter);
  assert.ok(parts.length >= 1);
  for (const seg of process.env.PATH.split(path.delimiter)) {
    if (seg) assert.ok(parts.includes(seg), `inherited segment missing: ${seg}`);
  }
});

test("isTrackedSelfEditLine: ignores untracked files and CodeGraph metadata", () => {
  assert.equal(bridge.isTrackedSelfEditLine("?? scratch.txt"), false);
  assert.equal(bridge.isTrackedSelfEditLine(" M results/run.sqlite"), false);
  assert.equal(bridge.isTrackedSelfEditLine(" M .codegraph/config.json"), false);
  assert.equal(bridge.isTrackedSelfEditLine(" M harness/server.py"), true);
});

test("statusPath: normalizes renamed and Windows-style paths", () => {
  assert.equal(bridge.statusPath(" M harness\\server.py"), "harness/server.py");
  assert.equal(bridge.statusPath("R  old.js -> webapp\\electron\\main.cjs"), "webapp/electron/main.cjs");
});

test("isUnmergedStatusLine: detects unresolved merge index states", () => {
  assert.equal(bridge.isUnmergedStatusLine("UU tests/test_verify.py"), true);
  assert.equal(bridge.isUnmergedStatusLine("AA harness/server.py"), true);
  assert.equal(bridge.isUnmergedStatusLine(" M harness/server.py"), false);
  assert.equal(bridge.isUnmergedStatusLine("?? scratch.py"), false);
});

test("mergeFailureLooksLikeStaleIndex: detects recoverable updater merge failures", () => {
  assert.equal(bridge.mergeFailureLooksLikeStaleIndex("error: could not write index"), true);
  assert.equal(bridge.mergeFailureLooksLikeStaleIndex("fatal: You have not concluded your merge (MERGE_HEAD exists)."), true);
  assert.equal(bridge.mergeFailureLooksLikeStaleIndex("fatal: Not possible to fast-forward, aborting."), false);
});

test("parseOrphanedAutoUpdateStashRefs: selects only marionette-auto-update entries", () => {
  const listed = [
    "stash@{0}: On main: WIP on main: abc1234 wip",
    "stash@{1}: On main: marionette-auto-update",
    "stash@{2}: WIP on feat: marionette-auto-update leftover",
    "",
  ].join("\n");
  assert.deepEqual(
    bridge.parseOrphanedAutoUpdateStashRefs(listed),
    ["stash@{1}", "stash@{2}"]
  );
  assert.deepEqual(bridge.parseOrphanedAutoUpdateStashRefs(""), []);
  assert.deepEqual(bridge.parseOrphanedAutoUpdateStashRefs("stash@{0}: On main: other"), []);
});

test("recoverOrphanedAutoUpdateStashes: reapplies a real git stash from a crash window", async () => {
  const { execFileSync } = require("node:child_process");
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "pmh-orphan-stash-"));
  const git = (args) =>
    execFileSync("git", ["-C", dir, ...args], {
      encoding: "utf8",
      env: {
        ...process.env,
        GIT_AUTHOR_NAME: "Marionette Test",
        GIT_AUTHOR_EMAIL: "test@marionette.local",
        GIT_COMMITTER_NAME: "Marionette Test",
        GIT_COMMITTER_EMAIL: "test@marionette.local",
      },
    });
  git(["init"]);
  git(["config", "user.email", "test@marionette.local"]);
  git(["config", "user.name", "Marionette Test"]);
  fs.writeFileSync(path.join(dir, "tracked.txt"), "base\n");
  git(["add", "tracked.txt"]);
  git(["commit", "-m", "init"]);
  fs.writeFileSync(path.join(dir, "tracked.txt"), "self-edit\n");
  git(["stash", "push", "-u", "-m", "marionette-auto-update"]);
  // Clean tree + orphaned stash = the crash window after stash push.
  assert.equal(fs.readFileSync(path.join(dir, "tracked.txt"), "utf8"), "base\n");
  const listed = git(["stash", "list"]);
  assert.match(listed, /marionette-auto-update/);

  const result = await bridge.recoverOrphanedAutoUpdateStashes(dir);
  assert.equal(result.recovered, 1);
  assert.equal(result.conflicts, 0);
  assert.equal(result.dropped, 0);
  assert.equal(fs.readFileSync(path.join(dir, "tracked.txt"), "utf8"), "self-edit\n");
  assert.equal(git(["stash", "list"]).trim(), "");
});

test("recoverOrphanedAutoUpdateStashes: conflict clears unmerged index and drops orphan", async () => {
  const { execFileSync } = require("node:child_process");
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "pmh-orphan-conflict-"));
  const git = (args) =>
    execFileSync("git", ["-C", dir, ...args], {
      encoding: "utf8",
      env: {
        ...process.env,
        GIT_AUTHOR_NAME: "Marionette Test",
        GIT_AUTHOR_EMAIL: "test@marionette.local",
        GIT_COMMITTER_NAME: "Marionette Test",
        GIT_COMMITTER_EMAIL: "test@marionette.local",
      },
    });
  git(["init"]);
  git(["config", "user.email", "test@marionette.local"]);
  git(["config", "user.name", "Marionette Test"]);
  fs.writeFileSync(path.join(dir, "tracked.txt"), "base\n");
  git(["add", "tracked.txt"]);
  git(["commit", "-m", "init"]);
  // Stash an edit, then commit a conflicting change on the same lines so
  // reapplying the orphan cannot succeed cleanly.
  fs.writeFileSync(path.join(dir, "tracked.txt"), "stashed-edit\n");
  git(["stash", "push", "-u", "-m", "marionette-auto-update"]);
  fs.writeFileSync(path.join(dir, "tracked.txt"), "upstream-edit\n");
  git(["add", "tracked.txt"]);
  git(["commit", "-m", "upstream"]);

  const result = await bridge.recoverOrphanedAutoUpdateStashes(dir);
  assert.equal(result.recovered, 0);
  assert.equal(result.conflicts, 1);
  assert.equal(result.dropped, 1);
  assert.equal(git(["stash", "list"]).trim(), "");
  // Working tree must not stay mid-merge / unmerged.
  const status = git(["status", "--porcelain"]);
  assert.equal(status.trim(), "");
  assert.equal(fs.readFileSync(path.join(dir, "tracked.txt"), "utf8"), "upstream-edit\n");
});

test("readLiveUpdateMarker: live pid within age ceiling is reported", () => {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "pmh-marker-"));
  marker.writeMarker(home, 4242, () => 1000_000);
  const live = marker.readLiveUpdateMarker(home, { kill: () => true, now: () => 1000_000 });
  assert.ok(live && live.pid === 4242);
});

test("readLiveUpdateMarker: dead pid is treated as no live update and the marker is cleared", () => {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "pmh-marker-"));
  marker.writeMarker(home, 4242);
  const deadKill = () => { const e = new Error("no such process"); e.code = "ESRCH"; throw e; };
  const live = marker.readLiveUpdateMarker(home, { kill: deadKill });
  assert.equal(live, null);
  assert.equal(fs.existsSync(marker.markerPath(home)), false);
});

test("readLiveUpdateMarker: a marker past the age ceiling self-heals", () => {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "pmh-marker-"));
  marker.writeMarker(home, 4242, () => 0); // started at t=0
  const live = marker.readLiveUpdateMarker(home, {
    kill: () => true,
    now: () => marker.UPDATE_MARKER_MAX_AGE_MS + 60_000,
  });
  assert.equal(live, null);
});

test("isElectronMainProcessFile: only webapp/electron/** counts as app-shell code", () => {
  assert.equal(bridge.isElectronMainProcessFile("webapp/electron/main.cjs"), true);
  assert.equal(bridge.isElectronMainProcessFile("webapp\\electron\\preload.cjs"), true);
  assert.equal(bridge.isElectronMainProcessFile("webapp/src/App.tsx"), false);
  assert.equal(bridge.isElectronMainProcessFile("harness/server.py"), false);
  assert.equal(bridge.isElectronMainProcessFile(""), false);
});

test("updateChangesElectronMain: flags a pulled range touching the app shell", () => {
  assert.equal(
    bridge.updateChangesElectronMain(["harness/server.py", "webapp/electron/main.cjs"]),
    true
  );
  assert.equal(
    bridge.updateChangesElectronMain(["harness/server.py", "webapp/src/App.tsx"]),
    false
  );
  assert.equal(bridge.updateChangesElectronMain([]), false);
  assert.equal(bridge.updateChangesElectronMain(null), false);
});

test("describeMainProcessUpdate: packaged installs need the latest installer", () => {
  const verdict = bridge.describeMainProcessUpdate({ mainProcessChanged: true, isPackaged: true });
  assert.equal(verdict.installerUpdateRequired, true);
  assert.match(verdict.note, /installer|latest Marionette release/i);
});

test("describeMainProcessUpdate: packaged shell skew alone also requires installer", () => {
  const verdict = bridge.describeMainProcessUpdate({
    mainProcessChanged: false,
    isPackaged: true,
    shellSkew: true,
  });
  assert.equal(verdict.installerUpdateRequired, true);
  assert.match(verdict.note, /app shell|installer/i);
});

test("describeMainProcessUpdate: source-run relaunch loads the new shell (no installer)", () => {
  const verdict = bridge.describeMainProcessUpdate({ mainProcessChanged: true, isPackaged: false });
  assert.equal(verdict.installerUpdateRequired, false);
  assert.match(verdict.note, /relaunch/i);
});

test("describeMainProcessUpdate: no shell change means nothing extra to require", () => {
  const verdict = bridge.describeMainProcessUpdate({ mainProcessChanged: false, isPackaged: true });
  assert.equal(verdict.installerUpdateRequired, false);
  assert.equal(verdict.note, undefined);
});

test("emptyApplyPlanResult: authoritative no-update is a no_update code", () => {
  const result = bridge.emptyApplyPlanResult({
    gitResult: { available: false, behind: 0 },
    packagedResult: { available: false },
  });
  assert.equal(result.ok, false);
  assert.equal(result.code, "no_update");
  assert.equal(result.error, "no update available");
});

test("emptyApplyPlanResult: a failed apply-time check is check_failed", () => {
  const gitFail = bridge.emptyApplyPlanResult({
    gitResult: { available: false, error: "git fetch failed" },
    packagedResult: { available: false },
  });
  assert.equal(gitFail.ok, false);
  assert.equal(gitFail.code, "check_failed");
  assert.equal(gitFail.error, "git fetch failed");

  const packagedFail = bridge.emptyApplyPlanResult({
    gitResult: { available: false, behind: 0 },
    packagedResult: { available: false, error: "electron-updater unavailable" },
  });
  assert.equal(packagedFail.code, "check_failed");
  assert.equal(packagedFail.error, "electron-updater unavailable");
});
