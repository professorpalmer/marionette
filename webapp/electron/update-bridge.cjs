// Self-update bridge: track the git checkout the app runs from, and update in
// place (git pull -> refresh deps -> rebuild renderer -> relaunch), Hermes-style.
//
// This replaces the older Tier-1 "download a DMG by hand" nudge. Marionette runs
// from a source checkout (the backend is the Python package under HARNESS_REPO;
// the renderer is served from webapp/dist), so an update is a `git pull` + a
// rebuild, not a full app reinstall. Merges to the tracked branch reach everyone
// on their next "Update & Relaunch" -- no signed DMG per change.
//
// Pattern lifted (with attribution) from the Hermes Agent desktop updater
// (MIT, Nous Research): passive HTTPS fetch to dodge passkey prompts, behind
// count with shallow-clone fallback, an in-progress marker, retry-once rebuild.
//
// Every git/npm/pip invocation streams its output to the renderer as progress.
// A failed check is SILENT (an update check must never nag). A failed apply
// surfaces a clear message and leaves the working tree as git left it.

const { spawn, execFile } = require("node:child_process");
const path = require("node:path");
const os = require("node:os");

const { chooseFetchRemote } = require("./update-remote.cjs");
const { resolveBehindCount, shouldCountCommits } = require("./update-count.cjs");
const { overallPercent } = require("./update-steps.cjs");
const { runRebuildWithRetry } = require("./update-rebuild.cjs");
const marker = require("./update-marker.cjs");

const DEFAULT_BRANCH = process.env.PMHARNESS_UPDATE_BRANCH || "main";
const REPO_HTML_URL = "https://github.com/professorpalmer/pm-harness";

function pmharnessHome() {
  return path.join(os.homedir(), ".pmharness");
}

// One-shot git capture: resolve { ok, out, err } (never rejects).
function gitCapture(repoRoot, args, timeoutMs = 30000) {
  return new Promise((resolve) => {
    execFile(
      "git",
      ["-C", repoRoot, ...args],
      { timeout: timeoutMs, maxBuffer: 10_000_000, encoding: "utf8" },
      (err, stdout, stderr) => {
        if (err) return resolve({ ok: false, out: (stdout || "").trim(), err: (stderr || String(err)).trim() });
        resolve({ ok: true, out: (stdout || "").trim(), err: (stderr || "").trim() });
      }
    );
  });
}

// Stream a child process, forwarding trimmed output lines to onLine, resolving
// { code, tail } where tail is the last non-empty line (useful for errors).
function runStreamed(cmd, args, opts, onLine) {
  return new Promise((resolve) => {
    let tail = "";
    let child;
    try {
      child = spawn(cmd, args, { ...opts });
    } catch (e) {
      onLine && onLine(String(e && e.message ? e.message : e));
      return resolve({ code: 1, tail: String(e) });
    }
    const onData = (buf) => {
      for (const raw of String(buf).split("\n")) {
        const line = raw.trimEnd();
        if (line.trim()) {
          tail = line.trim();
          onLine && onLine(line);
        }
      }
    };
    if (child.stdout) child.stdout.on("data", onData);
    if (child.stderr) child.stderr.on("data", onData);
    child.on("error", (e) => { onLine && onLine(String(e.message || e)); resolve({ code: 1, tail: String(e.message || e) }); });
    child.on("close", (code) => resolve({ code: code == null ? 1 : code, tail }));
  });
}

// Compare the running app version against the tracked branch. Fetches the tip
// (public HTTPS for the official SSH remote to avoid a passkey prompt), then
// resolves how many commits the checkout is behind. Never throws.
async function checkForUpdate({ repoRoot, branch = DEFAULT_BRANCH, currentVersion = "" }) {
  try {
    const origin = await gitCapture(repoRoot, ["config", "--get", "remote.origin.url"]);
    if (!origin.ok) return { available: false, error: "no git remote (not a checkout)" };

    const fetchRemote = chooseFetchRemote(origin.out);
    const fetched = await gitCapture(repoRoot, ["fetch", "--no-tags", fetchRemote, branch], 45000);
    if (!fetched.ok) return { available: false, error: fetched.err || "git fetch failed" };

    const cur = await gitCapture(repoRoot, ["rev-parse", "HEAD"]);
    const target = await gitCapture(repoRoot, ["rev-parse", "FETCH_HEAD"]);
    const currentSha = cur.ok ? cur.out : "";
    const targetSha = target.ok ? target.out : "";

    const shallow = await gitCapture(repoRoot, ["rev-parse", "--is-shallow-repository"]);
    const isShallow = shallow.ok && shallow.out === "true";
    const mergeBase = await gitCapture(repoRoot, ["merge-base", "HEAD", "FETCH_HEAD"]);
    const hasMergeBase = mergeBase.ok && !!mergeBase.out;

    let countStr = "";
    if (shouldCountCommits({ isShallow, hasMergeBase })) {
      const counted = await gitCapture(repoRoot, ["rev-list", "HEAD..FETCH_HEAD", "--count"]);
      countStr = counted.ok ? counted.out : "";
    }
    const behind = resolveBehindCount({ countStr, currentSha, targetSha, isShallow, hasMergeBase });

    return {
      available: behind > 0,
      behind,
      branch,
      currentSha: currentSha.slice(0, 8),
      targetSha: targetSha.slice(0, 8),
      currentVersion,
      url: REPO_HTML_URL,
    };
  } catch (e) {
    return { available: false, error: String(e && e.message ? e.message : e) };
  }
}

// Apply the update against the checkout: pull, refresh deps only if their
// lockfiles changed, rebuild the renderer. Streams progress via emit(stage,
// message, ratio). Returns { ok, error } -- on ok:true the caller relaunches.
async function applyUpdate({ repoRoot, branch = DEFAULT_BRANCH }, emit) {
  const home = pmharnessHome();
  marker.writeMarker(home);
  const progress = (stage, message, ratio = 0) =>
    emit && emit({ stage, message, percent: overallPercent(stage, ratio) });
  try {
    const beforeSha = (await gitCapture(repoRoot, ["rev-parse", "HEAD"])).out;

    // fetch
    progress("fetch", "Fetching latest changes", 0);
    const origin = await gitCapture(repoRoot, ["config", "--get", "remote.origin.url"]);
    if (!origin.ok) return { ok: false, error: "not a git checkout (no origin remote)" };
    const fetched = await runStreamed("git", ["-C", repoRoot, "fetch", "--no-tags", "origin", branch], {},
      (l) => progress("fetch", l, 0.5));
    if (fetched.code !== 0) return { ok: false, error: fetched.tail || "git fetch failed" };

    // pull (fast-forward only -- never rewrite the user's local work silently)
    progress("pull", "Updating source", 0);
    const pulled = await runStreamed("git", ["-C", repoRoot, "merge", "--ff-only", "FETCH_HEAD"], {},
      (l) => progress("pull", l, 0.5));
    if (pulled.code !== 0) {
      return {
        ok: false,
        error:
          "Could not fast-forward: you have local commits or uncommitted changes on this branch. " +
          "Commit/stash them or reset to origin/" + branch + ", then update again.",
      };
    }
    const afterSha = (await gitCapture(repoRoot, ["rev-parse", "HEAD"])).out;

    // deps -- only when their lockfiles actually changed between old and new HEAD
    progress("deps", "Checking dependencies", 0);
    const changed = beforeSha && afterSha
      ? (await gitCapture(repoRoot, ["diff", "--name-only", beforeSha, afterSha])).out.split("\n")
      : [];
    const pyChanged = changed.some((f) => /(^|\/)(pyproject\.toml|setup\.cfg|setup\.py|requirements[^/]*\.txt)$/.test(f));
    const nodeChanged = changed.some((f) => f === "webapp/package-lock.json" || f === "webapp/package.json");

    if (pyChanged) {
      progress("deps", "Updating Python dependencies", 0.3);
      const py = process.env.PMHARNESS_PYTHON || path.join(repoRoot, ".venv", "bin", "python");
      const pip = await runStreamed(py, ["-m", "pip", "install", "-e", ".", "--quiet"],
        { cwd: repoRoot }, (l) => progress("deps", l, 0.4));
      if (pip.code !== 0) return { ok: false, error: pip.tail || "pip install failed" };
    }
    if (nodeChanged) {
      progress("deps", "Updating node dependencies", 0.7);
      const npmci = await runStreamed("npm", ["ci"], { cwd: path.join(repoRoot, "webapp") },
        (l) => progress("deps", l, 0.8));
      if (npmci.code !== 0) return { ok: false, error: npmci.tail || "npm ci failed" };
    }

    // build -- rebuild the renderer into dist/. Retry once: a first build can
    // trip on a still-settling tree; the second is a near-no-op if the first won.
    const rebuild = async (attempt) => {
      progress("build", attempt === 0 ? "Rebuilding app" : "Rebuilding app (retry)", 0.1);
      return runStreamed("npm", ["run", "build"], { cwd: path.join(repoRoot, "webapp") },
        (l) => progress("build", l, 0.5));
    };
    const built = await runRebuildWithRetry(rebuild);
    if (built.code !== 0) return { ok: false, error: built.tail || "renderer build failed" };

    progress("done", "Update ready -- relaunching", 1);
    return { ok: true };
  } catch (e) {
    return { ok: false, error: String(e && e.message ? e.message : e) };
  } finally {
    marker.clearMarker(home);
  }
}

// Register IPC. `opts.getRepoRoot()` returns the checkout path; `opts.relaunch()`
// tears down the backend and re-execs the app.
function registerUpdateBridge(ipcMain, app, shell, opts = {}) {
  const getRepoRoot = opts.getRepoRoot || (() => path.join(os.homedir(), "pm-harness"));
  const relaunch = opts.relaunch || (() => { app.relaunch(); app.exit(0); });
  let applying = false;

  ipcMain.handle("updates:check", async () => {
    const currentVersion = app.getVersion();
    const res = await checkForUpdate({ repoRoot: getRepoRoot(), currentVersion });
    return { current: currentVersion, ...res };
  });

  ipcMain.handle("updates:apply", async (event) => {
    if (applying) return { ok: false, error: "an update is already in progress" };
    applying = true;
    const emit = (payload) => {
      try {
        if (event.sender && !event.sender.isDestroyed()) event.sender.send("updates:progress", payload);
      } catch { void 0; }
    };
    try {
      const result = await applyUpdate({ repoRoot: getRepoRoot() }, emit);
      if (result.ok) {
        // Give the renderer a beat to paint the final "relaunching" state.
        setTimeout(() => { try { relaunch(); } catch { void 0; } }, 400);
      }
      return result;
    } finally {
      applying = false;
    }
  });

  // Open the repo (or its commits) in the default browser.
  ipcMain.handle("updates:openRepo", async (_e, sub) => {
    const target = sub === "commits" ? `${REPO_HTML_URL}/commits/${DEFAULT_BRANCH}` : REPO_HTML_URL;
    try { await shell.openExternal(target); return true; } catch { return false; }
  });
}

module.exports = { registerUpdateBridge, checkForUpdate, applyUpdate };
