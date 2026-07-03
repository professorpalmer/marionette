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

test("planPuppetmasterUpgrade: a plain PyPI install upgrades to puppetmaster-ai latest", () => {
  const plan = pm.planPuppetmasterUpgrade({
    specEnv: "",
    pipShowOutput: "Name: puppetmaster-ai\nVersion: 1.1.0\nLocation: /app/.venv/lib/python3.11/site-packages",
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

test("buildUpdaterEnv: login-shell PATH is prepended so npm/uv resolve (fixes spawn ENOENT)", () => {
  const merged = env.buildUpdaterEnv({
    processEnv: { PATH: "/usr/bin:/bin", HARNESS_TOKEN: "keep-me" },
    shellEnv: { PATH: "/opt/homebrew/bin:/usr/bin", SSH_AUTH_SOCK: "/tmp/agent.sock" },
  });
  const parts = merged.PATH.split(require("node:path").delimiter);
  assert.equal(parts[0], "/opt/homebrew/bin", "homebrew (shell) dir comes first");
  assert.ok(parts.includes("/bin"), "base PATH dirs are preserved");
  assert.equal(parts.filter((p) => p === "/usr/bin").length, 1, "duplicate dirs are de-duplicated");
  assert.equal(merged.SSH_AUTH_SOCK, "/tmp/agent.sock", "shell-only vars are merged in");
  assert.equal(merged.HARNESS_TOKEN, "keep-me", "base env vars are preserved");
});

test("buildUpdaterEnv: an empty shell env leaves the base PATH intact", () => {
  const merged = env.buildUpdaterEnv({ processEnv: { PATH: "/usr/bin:/bin" }, shellEnv: {} });
  assert.equal(merged.PATH, "/usr/bin:/bin");
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
