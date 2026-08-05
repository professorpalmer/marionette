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
// The renderer sees calm stage labels only ("Updating source", "Rebuilding
// app"); raw git/npm/pip output goes to ~/.pmharness/update.log so deprecation
// warnings never scroll across the UI. A failed check is SILENT (an update
// check must never nag). A failed apply surfaces a clear message and leaves
// the working tree as git left it.

const { spawn, execFile } = require("node:child_process");
const path = require("node:path");
const os = require("node:os");

const { chooseFetchRemote } = require("./update-remote.cjs");
const { resolveBehindCount, shouldCountCommits } = require("./update-count.cjs");
const { overallPercent } = require("./update-steps.cjs");
const { runRebuildWithRetry } = require("./update-rebuild.cjs");
const { resolveCheckoutPin: resolvePuppetmasterPin } = require("./update-pm.cjs");
const { runtimeParityFields } = require("./puppetmaster-runtime.cjs");
const marker = require("./update-marker.cjs");
const {
  mergeUpdateAvailability,
  shouldRelaunchAfterSourceUpdate,
  planSeamlessApplyStages,
  readCheckoutPackageVersion,
  shellBehindCheckout,
} = require("./packaged-updater.cjs");

const DEFAULT_BRANCH = process.env.PMHARNESS_UPDATE_BRANCH || "main";
const REPO_HTML_URL = "https://github.com/professorpalmer/marionette";

function pmharnessHome() {
  return path.join(os.homedir(), ".pmharness");
}

// Raw child-process output (npm warnings, git chatter, pip noise) goes to a log
// file, NOT the update banner. Deprecation warnings and dependency chatter read
// like something is broken when they scroll across the UI; the banner shows only
// calm stage labels, and this file keeps the full transcript for debugging.
function appendUpdateLog(line) {
  try {
    require("node:fs").appendFileSync(
      path.join(pmharnessHome(), "update.log"),
      `${new Date().toISOString()} ${line}\n`
    );
  } catch { /* logging must never break an update */ }
}

function hiddenProcessOptions(opts = {}) {
  return { windowsHide: true, ...opts };
}

// One-shot git capture: resolve { ok, out, err } (never rejects). `env`, when
// given, is the login-shell-augmented environment so a Finder-launched app can
// still resolve git and reach an SSH remote.
function gitCapture(repoRoot, args, timeoutMs = 30000, env) {
  return new Promise((resolve) => {
    execFile(
      "git",
      ["-C", repoRoot, ...args],
      hiddenProcessOptions({ timeout: timeoutMs, maxBuffer: 10_000_000, encoding: "utf8", ...(env ? { env } : {}) }),
      (err, stdout, stderr) => {
        if (err) return resolve({ ok: false, out: (stdout || "").trim(), err: (stderr || String(err)).trim() });
        resolve({ ok: true, out: (stdout || "").trim(), err: (stderr || "").trim() });
      }
    );
  });
}

// One-shot process capture: resolve { ok, out, err } (never rejects). Used to
// read `pip show` output when deciding whether to upgrade Puppetmaster.
function execCapture(cmd, args, { timeoutMs = 30000, env } = {}) {
  return new Promise((resolve) => {
    execFile(
      cmd,
      args,
      hiddenProcessOptions({ timeout: timeoutMs, maxBuffer: 10_000_000, encoding: "utf8", ...(env ? { env } : {}) }),
      (err, stdout, stderr) => {
        resolve({ ok: !err, out: (stdout || "").trim(), err: (stderr || String(err || "")).trim() });
      }
    );
  });
}

// Is `uv` on PATH? (Marionette venvs are made by `uv venv`, which omits pip, so
// the updater prefers `uv pip ...` and only falls back to `python -m pip`.) Uses
// the augmented env so a Finder launch can find a Homebrew/curl-installed uv.
function detectUv(env) {
  return new Promise((resolve) => {
    execFile("uv", ["--version"], hiddenProcessOptions({ timeout: 5000, ...(env ? { env } : {}) }), (err) => resolve(!err));
  });
}

function statusPath(line) {
  const raw = line.slice(3).trim();
  const renamed = raw.split(" -> ").pop();
  return renamed.replace(/^"|"$/g, "").replace(/\\/g, "/");
}

function isTrackedSelfEditLine(line) {
  if (!line.trim() || line.startsWith("??")) return false;
  const file = statusPath(line);
  return !(
    file.startsWith("results/") ||
    file.startsWith(".codegraph/")
  );
}

function isUnmergedStatusLine(line) {
  const xy = line.slice(0, 2);
  return ["DD", "AU", "UD", "UA", "DU", "AA", "UU"].includes(xy);
}

const AUTO_UPDATE_STASH_MARK = "marionette-auto-update";

/** Parse `git stash list` lines that belong to a prior Marionette auto-update. */
function parseOrphanedAutoUpdateStashRefs(stashListOut) {
  const refs = [];
  for (const line of String(stashListOut || "").split(/\r?\n/)) {
    if (!line.includes(AUTO_UPDATE_STASH_MARK)) continue;
    const m = line.match(/^(stash@\{\d+\})\s*:/);
    if (m) refs.push(m[1]);
  }
  return refs;
}

/**
 * Recover stashes left by a crash between `stash push` and `stash pop`.
 * Tries oldest-first so newer unrelated stashes stay on top of the stack.
 *
 * A conflicting reapply must NOT leave the checkout mid-merge: that wedges
 * every later Restart into `conflict` → browser open. Clear unmerged index
 * state and drop the orphan (it is leftover auto-update noise, not user WIP).
 * Returns { recovered, conflicts, dropped, refs }.
 */
async function recoverOrphanedAutoUpdateStashes(repoRoot, env) {
  const listed = await gitCapture(repoRoot, ["stash", "list"], 15000, env);
  if (!listed.ok || !listed.out) {
    return { recovered: 0, conflicts: 0, dropped: 0, refs: [] };
  }
  const refs = parseOrphanedAutoUpdateStashRefs(listed.out);
  if (!refs.length) return { recovered: 0, conflicts: 0, dropped: 0, refs: [] };
  // Pop from the bottom of the matching set: highest stash@{N} first so
  // indices remain stable as we apply (git renumbers after each drop/pop).
  const ordered = refs.slice().sort((a, b) => {
    const na = parseInt(a.replace(/\D/g, ""), 10);
    const nb = parseInt(b.replace(/\D/g, ""), 10);
    return nb - na;
  });
  let recovered = 0;
  let conflicts = 0;
  let dropped = 0;
  for (const ref of ordered) {
    appendUpdateLog(`[stash] recovering orphaned ${ref} (${AUTO_UPDATE_STASH_MARK})`);
    const pop = await gitCapture(repoRoot, ["stash", "pop", ref], 60000, env);
    if (pop.ok) {
      recovered += 1;
      continue;
    }
    conflicts += 1;
    appendUpdateLog(`[stash] recover conflict on ${ref}: ${pop.err || pop.out || "failed"}`);
    // Failed `stash pop` leaves unmerged paths and keeps the stash entry.
    // Reset the index, then drop the orphan so the next apply can FF-pull.
    const cleared = await recoverInterruptedMerge(repoRoot);
    appendUpdateLog(
      `[stash] cleared unmerged state after conflict: ${cleared.ok ? "ok" : cleared.error || "failed"}`
    );
    const drop = await gitCapture(repoRoot, ["stash", "drop", ref], 15000, env);
    if (drop.ok) {
      dropped += 1;
      appendUpdateLog(`[stash] dropped conflicting orphan ${ref}`);
    } else {
      appendUpdateLog(`[stash] could not drop ${ref}: ${drop.err || drop.out || "failed"}`);
      // Stop so we do not thrash remaining refs against a still-wedged tree.
      break;
    }
  }
  return { recovered, conflicts, dropped, refs: ordered };
}

function mergeFailureLooksLikeStaleIndex(text) {
  return /could not write index|needs merge|unmerged files|you have not concluded your merge|merge_head/i.test(text || "");
}

// ---- Electron main-process update-skew detection ---------------------------
// The in-place update refreshes the CHECKOUT (Python backend + webapp/dist),
// but the Electron main process the user is running came from the installed
// app bundle (app.asar on packaged installs), frozen at install time. When an
// update changes webapp/electron/**, a relaunch of the packaged shell still
// runs the OLD main-process code against the NEW backend -- exactly the
// v0.9.95 skew where a pre-header-auth main kept sending `?token=` at a
// header-only backend. Detect that case so the result never claims an
// in-place renderer/backend refresh was enough.

/** True for files that ship inside the packaged Electron shell. */
function isElectronMainProcessFile(file) {
  return typeof file === "string" && file.replace(/\\/g, "/").startsWith("webapp/electron/");
}

/** True when the pulled range touched Electron main-process code. */
function updateChangesElectronMain(changedFiles) {
  return (changedFiles || []).some(isElectronMainProcessFile);
}

/**
 * Decide what a main-process change means for THIS install.
 * Packaged: main.cjs lives in app.asar -- a relaunch cannot load the new shell
 * code, so the user needs the latest installer. Source-run: a full app
 * relaunch re-reads webapp/electron from disk, so relaunch is sufficient
 * (but an in-place backend/renderer refresh alone is not).
 */
function describeMainProcessUpdate({ mainProcessChanged, isPackaged, shellSkew = false }) {
  if (!mainProcessChanged && !shellSkew) return { installerUpdateRequired: false };
  if (isPackaged && (mainProcessChanged || shellSkew)) {
    return {
      installerUpdateRequired: true,
      note:
        "This update also changes the Marionette app shell, which the installed " +
        "app cannot refresh in place. Installing the latest Marionette release " +
        "finishes the update; until then the source checkout is current but the " +
        "packaged shell is still waiting on the installer.",
    };
  }
  if (mainProcessChanged) {
    return {
      installerUpdateRequired: false,
      note: "This update changes the app shell; the full app relaunch will load it.",
    };
  }
  return { installerUpdateRequired: false };
}

async function recoverInterruptedMerge(repoRoot) {
  const status = await gitCapture(repoRoot, ["status", "--porcelain"]);
  const hasUnmerged = status.ok && status.out.split("\n").some(isUnmergedStatusLine);
  const mergeHead = await gitCapture(repoRoot, ["rev-parse", "-q", "--verify", "MERGE_HEAD"]);
  if (!hasUnmerged && !mergeHead.ok) return { recovered: false, ok: true };

  const aborted = await gitCapture(repoRoot, ["merge", "--abort"]);
  if (aborted.ok) return { recovered: true, ok: true };

  // Some failed self-updates leave unmerged index entries but no abortable merge
  // metadata. `reset --merge` restores the index/working tree to HEAD without
  // moving local commits, which is exactly the stale updater state we need to
  // clear before retrying the fast-forward.
  const reset = await gitCapture(repoRoot, ["reset", "--merge"]);
  return { recovered: reset.ok, ok: reset.ok, error: reset.err || aborted.err };
}

// Inspect the working tree so the updater can tell a clean checkout apart from
// one the user (or Marionette editing itself) has modified. `dirty` = tracked
// changes exist besides results/ (which is gitignored churn). `ahead` = local
// commits not on the tracked upstream. Both drive the diverged-tree update UX:
// a dirty tree can be stashed + reapplied, but an ahead/diverged tree needs the
// user to rebase or reset -- we never rewrite their commits silently.
async function inspectTree(repoRoot, branch) {
  const status = await gitCapture(repoRoot, ["status", "--porcelain"]);
  // Only TRACKED modifications count as dirty. Untracked files ("?? ") cannot
  // block a fast-forward merge, and the pilot routinely drops scratch files
  // (analysis scripts, result dumps) into the checkout -- counting those made
  // every update nag "you have local self-edits" forever.
  const dirtyFiles = status.ok
    ? status.out.split("\n").filter(isTrackedSelfEditLine).map(statusPath)
    : [];
  const dirty = dirtyFiles.length > 0;
  // Commits on HEAD that FETCH_HEAD (the fetched branch tip) doesn't contain.
  const aheadRes = await gitCapture(repoRoot, ["rev-list", "--count", "FETCH_HEAD..HEAD"]);
  const ahead = aheadRes.ok ? (parseInt(aheadRes.out, 10) || 0) : 0;
  return { dirty, dirtyFiles, ahead };
}

// Stream a child process, forwarding trimmed output lines to onLine, resolving
// { code, tail } where tail is the last non-empty line (useful for errors).
// npm on Windows is a .cmd shim that Node will not spawn without shell:true
// (spawn errors out and close reports code null). Stream npm through a shell
// on win32; everywhere else spawn it directly.
function runNpmStreamed(args, opts, onLine) {
  return process.platform === "win32"
    ? runStreamed("npm.cmd", args, { ...opts, shell: true }, onLine)
    : runStreamed("npm", args, opts, onLine);
}

function runStreamed(cmd, args, opts, onLine) {
  return new Promise((resolve) => {
    let tail = "";
    let child;
    try {
      child = spawn(cmd, args, hiddenProcessOptions(opts));
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
async function checkForUpdate({ repoRoot, branch = DEFAULT_BRANCH, currentVersion = "", env }) {
  try {
    const origin = await gitCapture(repoRoot, ["config", "--get", "remote.origin.url"]);
    if (!origin.ok) return { available: false, error: "no git remote (not a checkout)" };

    const fetchRemote = chooseFetchRemote(origin.out);
    const fetched = await gitCapture(repoRoot, ["fetch", "--no-tags", fetchRemote, branch], 45000, env);
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

    // Tree state so the UI can pick an apply strategy up front (a self-edited
    // checkout is dirty and/or ahead of origin).
    const { dirty, dirtyFiles, ahead } = await inspectTree(repoRoot, branch);

    // Version string at the fetched tip, so the banner can say "v0.7.35 is
    // ready" instead of a generic label (or worse, the branch name).
    let latest = "";
    const pkg = await gitCapture(repoRoot, ["show", "FETCH_HEAD:webapp/package.json"]);
    if (pkg.ok) {
      try { latest = JSON.parse(pkg.out).version || ""; } catch { /* leave empty */ }
    }

    const stashList = await gitCapture(repoRoot, ["stash", "list"], 15000, env);
    const orphanedAutoUpdateStashes = parseOrphanedAutoUpdateStashRefs(
      stashList.ok ? stashList.out : ""
    );

    return {
      available: behind > 0,
      behind,
      latest,
      branch,
      currentSha: currentSha.slice(0, 8),
      targetSha: targetSha.slice(0, 8),
      currentVersion,
      dirty,
      ahead,
      orphanedAutoUpdateStashes: orphanedAutoUpdateStashes.length,
      url: REPO_HTML_URL,
    };
  } catch (e) {
    return { available: false, error: String(e && e.message ? e.message : e) };
  }
}

// Apply the update against the checkout: pull, refresh deps only if their
// lockfiles changed, rebuild the renderer. Streams progress via emit(stage,
// message, ratio). Returns { ok, error, code } -- on ok:true the caller
// relaunches.
//
// strategy resolves the self-edit vs update collision (Marionette can edit its
// own source, so the tree is often dirty/ahead):
//   "ff"    (default) -- fast-forward only; refuse on a dirty/diverged tree and
//                         return a structured { code } so the UI can offer a choice.
//   "stash" -- set aside uncommitted edits (git stash -u), fast-forward, then
//              reapply them (git stash pop) before the rebuild, so self-edits
//              survive the update.
// A tree that is *ahead* (local commits) can never fast-forward; that is a real
// fork divergence and always returns code:"diverged" -- we never rewrite commits.
async function applyUpdate({ repoRoot, branch = DEFAULT_BRANCH, strategy = "ff", env }, emit) {
  const home = pmharnessHome();
  marker.writeMarker(home);
  // Login-shell-augmented env so a Finder/Dock launch (stripped launchd PATH)
  // can still find npm/uv/git and reach an SSH remote. Falls back to the inherited
  // env when the caller does not supply one (dev/CLI runs already have a full env).
  const childEnv = env || process.env;
  const progress = (stage, message, ratio = 0) =>
    emit && emit({ stage, message, percent: overallPercent(stage, ratio) });
  let stashed = false;
  try {
    // Clear any mid-merge / unmerged index left by a prior failed stash pop
    // before touching orphan stashes — otherwise recover itself cannot write
    // the index and every Restart hard-fails into the browser fallback.
    progress("pull", "Checking checkout state", 0.02);
    const preRecover = await recoverInterruptedMerge(repoRoot);
    if (!preRecover.ok) {
      return {
        ok: false,
        code: "conflict",
        error:
          preRecover.error ||
          "A previous update left the checkout mid-merge. Resolve git status in the Marionette checkout, then update again.",
      };
    }
    if (preRecover.recovered) {
      appendUpdateLog("[pull] cleared interrupted merge / unmerged index before update");
    }

    // Crash between a prior stash push and pop leaves `marionette-auto-update`
    // entries on the stash stack forever. Reapply them before starting a new
    // update so the next run does not treat the tree as clean and orphan edits.
    // Conflicting orphans are dropped after clearing the tree (see recover).
    progress("pull", "Checking for interrupted update stash", 0.05);
    const orphanRecovery = await recoverOrphanedAutoUpdateStashes(repoRoot, childEnv);
    if (orphanRecovery.recovered > 0) {
      appendUpdateLog(
        `[stash] reapplied ${orphanRecovery.recovered} orphaned ${AUTO_UPDATE_STASH_MARK} stash(es)`
      );
    }
    if (orphanRecovery.dropped > 0) {
      appendUpdateLog(
        `[stash] dropped ${orphanRecovery.dropped} conflicting orphan ${AUTO_UPDATE_STASH_MARK} stash(es); continuing update`
      );
    }

    const beforeSha = (await gitCapture(repoRoot, ["rev-parse", "HEAD"])).out;

    // fetch
    progress("fetch", "Fetching latest changes", 0);
    const origin = await gitCapture(repoRoot, ["config", "--get", "remote.origin.url"]);
    if (!origin.ok) return { ok: false, error: "not a git checkout (no origin remote)" };
    const fetched = await runStreamed("git", ["-C", repoRoot, "fetch", "--no-tags", "origin", branch], { env: childEnv },
      (l) => { appendUpdateLog(`[fetch] ${l}`); progress("fetch", "Fetching latest changes", 0.5); });
    if (fetched.code !== 0) return { ok: false, error: fetched.tail || "git fetch failed" };

    const recovered = await recoverInterruptedMerge(repoRoot);
    if (!recovered.ok) {
      return {
        ok: false,
        code: "conflict",
        error: recovered.error || "A previous update left the checkout mid-merge. Resolve git status in the Marionette checkout, then update again.",
      };
    }

    // Diverged/dirty preflight: decide whether we can fast-forward at all.
    const { dirty, dirtyFiles, ahead } = await inspectTree(repoRoot, branch);
    if (ahead > 0) {
      return {
        ok: false,
        code: "diverged",
        error:
          `Your checkout has ${ahead} local commit(s) that aren't on origin/${branch} ` +
          `(a diverged fork). Rebase onto origin/${branch} or reset --hard origin/${branch}, then update again.`,
      };
    }
    if (dirty && strategy !== "stash") {
      return {
        ok: false,
        code: "dirty",
        error:
          "You have uncommitted changes (self-edits): " +
          (dirtyFiles.length ? dirtyFiles.slice(0, 6).join(", ") : "tracked files changed") +
          (dirtyFiles.length > 6 ? `, and ${dirtyFiles.length - 6} more` : "") +
          ". Choose 'Stash & update' to set them aside and reapply them after updating, or commit them first.",
      };
    }
    if (dirty && strategy === "stash") {
      progress("pull", "Stashing local self-edits", 0.1);
      const st = await gitCapture(repoRoot, ["stash", "push", "-u", "-m", "marionette-auto-update"]);
      if (!st.ok) return { ok: false, error: st.err || "git stash failed" };
      stashed = true;
    }

    // pull (fast-forward only -- never rewrite the user's local work silently)
    progress("pull", "Updating source", 0.3);
    let pulled = await runStreamed("git", ["-C", repoRoot, "merge", "--ff-only", "FETCH_HEAD"], { env: childEnv },
      (l) => { appendUpdateLog(`[pull] ${l}`); progress("pull", "Updating source", 0.5); });
    if (pulled.code !== 0 && mergeFailureLooksLikeStaleIndex(pulled.tail)) {
      progress("pull", "Repairing stale update state", 0.55);
      const repaired = await recoverInterruptedMerge(repoRoot);
      if (repaired.ok) {
        pulled = await runStreamed("git", ["-C", repoRoot, "merge", "--ff-only", "FETCH_HEAD"], { env: childEnv },
          (l) => { appendUpdateLog(`[pull] ${l}`); progress("pull", "Updating source", 0.65); });
      }
    }
    if (pulled.code !== 0) {
      // Restore stashed edits before surfacing the failure so we never strand
      // the user's work in the stash on a failed update.
      if (stashed) { await gitCapture(repoRoot, ["stash", "pop"]); stashed = false; }
      return {
        ok: false,
        code: "diverged",
        error:
          "Could not fast-forward onto origin/" + branch + ". Commit/stash your changes or " +
          "reset to origin/" + branch + ", then update again.",
      };
    }

    // Reapply the stashed self-edits onto the updated source before rebuilding,
    // so the new build includes them. A conflict here means the upstream change
    // touched the same lines -- surface it clearly instead of silently dropping.
    if (stashed) {
      progress("pull", "Reapplying local self-edits", 0.8);
      const pop = await gitCapture(repoRoot, ["stash", "pop"]);
      stashed = false;
      if (!pop.ok) {
        return {
          ok: false,
          code: "conflict",
          error:
            "Updated, but your self-edits conflict with the new code. Resolve the conflict in " +
            repoRoot + " (git status), then rebuild. Your changes are in the working tree.",
        };
      }
    }
    const afterSha = (await gitCapture(repoRoot, ["rev-parse", "HEAD"])).out;

    // deps -- only when their lockfiles actually changed between old and new HEAD
    progress("deps", "Checking dependencies", 0);
    const changed = beforeSha && afterSha
      ? (await gitCapture(repoRoot, ["diff", "--name-only", beforeSha, afterSha])).out.split("\n")
      : [];
    const pyChanged = changed.some((f) => /(^|\/)(pyproject\.toml|setup\.cfg|setup\.py|requirements[^/]*\.txt)$/.test(f));
    const nodeChanged = changed.some((f) => f === "webapp/package-lock.json" || f === "webapp/package.json");
    const mainProcessChanged = updateChangesElectronMain(changed);

    const py = process.env.PMHARNESS_PYTHON || (process.platform === "win32"
      ? path.join(repoRoot, ".venv", "Scripts", "python.exe")
      : path.join(repoRoot, ".venv", "bin", "python"));
    // Marionette venvs are created by `uv venv`, which does NOT install pip, so
    // prefer `uv pip ...`. Fall back to `python -m pip` for an older pip-bearing
    // venv. Detected once and reused for both the app and the Puppetmaster step.
    const hasUv = await detectUv(childEnv);

    if (pyChanged) {
      progress("deps", "Updating Python dependencies", 0.3);
      const onDepLine = (l) => { appendUpdateLog(`[deps] ${l}`); progress("deps", "Updating Python dependencies", 0.4); };
      const dep = hasUv
        ? await runStreamed("uv", ["pip", "install", "--python", py, "-e", "."],
            { cwd: repoRoot, env: childEnv }, onDepLine)
        : await runStreamed(py, ["-m", "pip", "install", "-e", ".", "--quiet"],
            { cwd: repoRoot, env: childEnv }, onDepLine);
      if (dep.code !== 0) return { ok: false, error: dep.tail || "python dependency install failed" };
    }
    if (nodeChanged) {
      progress("deps", "Updating node dependencies", 0.7);
      const npmci = await runNpmStreamed(["ci"], { cwd: path.join(repoRoot, "webapp"), env: childEnv },
        (l) => { appendUpdateLog(`[deps] ${l}`); progress("deps", "Updating node dependencies", 0.8); });
      if (npmci.code !== 0) return { ok: false, error: npmci.tail || "npm ci failed" };
    }

    // Puppetmaster -- the one integral runtime dep -- ships independently of this
    // repo (separately versioned PyPI package), so a git pull never carries a PM release.
    // Upgrade it on every update so overhauls reach existing installs, unless a
    // dev/custom spec owns it. Non-fatal: a PyPI blip or offline machine must
    // never strand an otherwise-successful app update -- PM just stays put.
    // Show by dist name (not ==pin): `uv pip show pkg==X` fails when an older
    // version is installed, which made the updater look like a no-op and left
    // app venvs stuck on stale Puppetmaster (1.20.10 after a 1.21.1 pin bump).
    progress("deps", "Checking Puppetmaster", 0.85);
    // Prefer the checkout's pin (post-pull) over the packaged shell's frozen pin.
    const pmPin = resolvePuppetmasterPin(repoRoot);
    const pmShow = hasUv
      ? await execCapture("uv", ["pip", "show", "--python", py, pmPin.distName], { env: childEnv })
      : await execCapture(py, ["-m", "pip", "show", pmPin.distName], { env: childEnv });
    const pmPlan = pmPin.planPuppetmasterUpgrade({
      specEnv: process.env.MARIONETTE_PUPPETMASTER_SPEC,
      pipShowOutput: pmShow.out,
      pinnedSpec: pmPin.pinnedSpec,
    });
    appendUpdateLog(
      `[deps] Puppetmaster plan: skip=${pmPlan.skip}`
      + (pmPlan.reason ? ` reason=${pmPlan.reason}` : "")
      + (pmPlan.have || pmPlan.want ? ` have=${pmPlan.have || "?"} want=${pmPlan.want || "?"}` : "")
      + ` pin=${pmPin.pinnedSpec}`
    );
    if (pmPlan.skip) {
      progress("deps", "Puppetmaster: " + pmPlan.reason + ", leaving as-is", 0.9);
    } else {
      progress("deps", "Updating Puppetmaster", 0.9);
      const onPmLine = (l) => { appendUpdateLog(`[deps] ${l}`); progress("deps", "Updating Puppetmaster", 0.92); };
      const pm = hasUv
        ? await runStreamed("uv", ["pip", "install", "--python", py, "--upgrade", pmPlan.spec],
            { cwd: repoRoot, env: childEnv }, onPmLine)
        : await runStreamed(py, ["-m", "pip", "install", "--upgrade", pmPlan.spec, "--quiet"],
            { cwd: repoRoot, env: childEnv }, onPmLine);
      if (pm.code !== 0) {
        appendUpdateLog(`[deps] Puppetmaster upgrade skipped: ${pm.tail || "unavailable"}`);
        progress("deps", "Puppetmaster upgrade skipped", 0.95);
      } else {
        appendUpdateLog(`[deps] Puppetmaster upgraded to ${pmPlan.spec}`);
      }
    }

    // build -- rebuild the renderer into dist/. Retry once: a first build can
    // trip on a still-settling tree; the second is a near-no-op if the first won.
    const rebuild = async (attempt) => {
      const label = attempt === 0 ? "Rebuilding app" : "Rebuilding app (retry)";
      progress("build", label, 0.1);
      return runNpmStreamed(["run", "build"], { cwd: path.join(repoRoot, "webapp"), env: childEnv },
        (l) => { appendUpdateLog(`[build] ${l}`); progress("build", label, 0.5); });
    };
    const built = await runRebuildWithRetry(rebuild);
    if (built.code !== 0) return { ok: false, error: built.tail || "renderer build failed" };

    progress(
      "done",
      mainProcessChanged
        ? "Update ready -- relaunching (app shell changed; full restart required)"
        : "Update ready -- relaunching",
      1
    );
    return { ok: true, mainProcessChanged };
  } catch (e) {
    return { ok: false, error: String(e && e.message ? e.message : e) };
  } finally {
    // Never leave the user's self-edits trapped in the stash if we bailed out
    // between the stash push and its pop.
    if (stashed) { try { await gitCapture(repoRoot, ["stash", "pop"]); } catch { /* leave in stash */ } }
    marker.clearMarker(home);
  }
}

// Register IPC. `opts.getRepoRoot()` returns the checkout path; `opts.relaunch()`
// tears down the backend and re-execs the app. Packaged installs also get
// `opts.packagedUpdater` (electron-updater) so shell skew is resolved via a
// signed installer instead of a false-success relaunch of the frozen asar.
function registerUpdateBridge(ipcMain, app, shell, opts = {}) {
  const getRepoRoot = opts.getRepoRoot || (() => path.join(os.homedir(), "pm-harness"));
  const relaunch = opts.relaunch || (() => { app.relaunch(); app.exit(0); });
  // Login-shell-augmented env for the updater's child processes (git/npm/uv), so
  // a Finder/Dock launch with a stripped launchd PATH can still find them. Omit
  // to inherit process.env (dev/CLI runs already have a full env).
  const getEnv = opts.getEnv || (() => process.env);
  const packagedUpdater = opts.packagedUpdater || null;
  // Startup Puppetmaster parity result (puppetmaster-runtime.cjs). A stale
  // runtime is an update the user still owes, so the check payload carries it.
  const getRuntimeParity = opts.getRuntimeParity || (() => null);
  // Broadcast to every window: the update watcher is a main-process concern, so
  // it must not depend on which renderer happened to invoke something last.
  const broadcast = opts.broadcast || ((channel, payload) => {
    try {
      const { BrowserWindow } = require("electron");
      for (const win of BrowserWindow.getAllWindows()) {
        if (!win.isDestroyed()) win.webContents.send(channel, payload);
      }
    } catch { /* window gone mid-send */ }
  });
  let applying = false;

  const doCheck = async () => {
    const currentVersion = app.getVersion();
    const repoRoot = getRepoRoot();
    const checkoutVersion = readCheckoutPackageVersion(repoRoot);
    const gitRes = await checkForUpdate({ repoRoot, currentVersion, env: getEnv() });
    let packagedRes = null;
    if (packagedUpdater && packagedUpdater.enabled) {
      try {
        packagedRes = await packagedUpdater.check();
      } catch (err) {
        appendUpdateLog(`[packaged] check failed: ${err && err.message ? err.message : err}`);
      }
    }
    const merged = mergeUpdateAvailability({
      gitResult: gitRes,
      packagedResult: packagedRes,
      isPackaged: !!(app && app.isPackaged),
      shellVersion: currentVersion,
      checkoutVersion,
    });
    return { current: currentVersion, ...merged, ...runtimeParityFields(getRuntimeParity()) };
  };

  // PUSH model: the main process owns the update watcher and notifies every
  // renderer the moment a fetch sees new commits. Renderer-side polling proved
  // unreliable (mount-once effects, throttles, and a hidden window meant the
  // pill often appeared only after a full app restart). One timer here survives
  // renderer reloads and never depends on window focus. Silent by design: a
  // failed background check must not nag.
  const WATCH_INTERVAL_MS = 15 * 60 * 1000;
  const FIRST_CHECK_DELAY_MS = 15 * 1000; // let startup I/O settle first
  let watching = false;
  const watchTick = async () => {
    if (applying) return; // never fetch mid-apply; the relaunch re-arms us
    try {
      const res = await doCheck();
      if (res && res.available) broadcast("updates:available", res);
    } catch { /* silent */ }
  };
  const startWatcher = () => {
    if (watching || process.env.MARIONETTE_UPDATE_WATCH === "0") return;
    watching = true;
    const first = setTimeout(watchTick, FIRST_CHECK_DELAY_MS);
    const interval = setInterval(watchTick, WATCH_INTERVAL_MS);
    first.unref?.();
    interval.unref?.();
  };
  startWatcher();

  ipcMain.handle("updates:check", doCheck);

  ipcMain.handle("updates:apply", async (event, arg) => {
    if (applying) return { ok: false, error: "an update is already in progress" };
    applying = true;
    // arg may be a strategy string ("ff"|"stash") or an options object.
    const strategy = (arg && typeof arg === "object" ? arg.strategy : arg) || "ff";
    const emit = (payload) => {
      try {
        if (event.sender && !event.sender.isDestroyed()) event.sender.send("updates:progress", payload);
      } catch { void 0; }
    };
    const isPackaged = !!(app && app.isPackaged);
    try {
      // One Restart click owns both planes: checkout (git) first, then packaged
      // shell via electron-updater. Never install the shell alone while git is
      // still behind -- that was the double Restart / shell-skew loop.
      const repoRoot = getRepoRoot();
      const shellVersion = app.getVersion();
      let checkoutVersion = readCheckoutPackageVersion(repoRoot);
      let skew = isPackaged && shellBehindCheckout({ shellVersion, checkoutVersion });

      const gitRes = await checkForUpdate({
        repoRoot,
        currentVersion: shellVersion,
        env: getEnv(),
      });
      let packagedRes = null;
      if (packagedUpdater && packagedUpdater.enabled) {
        try {
          packagedRes = await packagedUpdater.check();
        } catch (err) {
          appendUpdateLog(`[packaged] pre-apply check failed: ${err && err.message ? err.message : err}`);
        }
      }

      let plan = planSeamlessApplyStages({
        isPackaged,
        gitAvailable: !!(gitRes && gitRes.available),
        packagedAvailable: !!(packagedRes && packagedRes.available),
        packagedDownloaded: !!(
          (packagedUpdater && packagedUpdater.isDownloaded()) ||
          (packagedRes && packagedRes.downloaded)
        ),
        shellSkew: skew,
      });

      let sourceResult = { ok: true, skipped: true, mainProcessChanged: false };
      if (plan.runSource) {
        sourceResult = await applyUpdate({ repoRoot, strategy, env: getEnv() }, emit);
        if (!sourceResult.ok) {
          // Source plane failed, but a packaged shell update can still finish
          // the user-visible "update Marionette" path. Prefer that over opening
          // GitHub when electron-updater already has a newer build ready.
          const canContinueShell =
            plan.runShell && packagedUpdater && packagedUpdater.enabled;
          if (!canContinueShell) return sourceResult;
          appendUpdateLog(
            `[apply] source failed (${sourceResult.code || "error"}); continuing with packaged shell install`
          );
          sourceResult = {
            ok: true,
            skipped: true,
            mainProcessChanged: false,
            sourceError: sourceResult.error || sourceResult.code || "source update failed",
          };
        } else {
          checkoutVersion = readCheckoutPackageVersion(repoRoot);
          skew = isPackaged && shellBehindCheckout({ shellVersion, checkoutVersion });
          // After pull, main-process or version skew may newly require the installer.
          if (isPackaged && (skew || sourceResult.mainProcessChanged)) {
            plan = { ...plan, runShell: true, sequence: [...plan.sequence.filter((s) => s !== "shell"), "shell"] };
          }
        }
      }

      if (plan.runShell) {
        const shellVerdict = describeMainProcessUpdate({
          mainProcessChanged: !!sourceResult.mainProcessChanged,
          isPackaged,
          shellSkew: skew,
        });
        if (packagedUpdater && packagedUpdater.enabled) {
          appendUpdateLog(`[apply] seamless shell stage after source=${!sourceResult.skipped}`);
          emit({
            stage: "install",
            message: sourceResult.skipped
              ? "Installing app shell update — Marionette will relaunch"
              : "Checkout updated — installing app shell",
            percent: 96,
          });
          try {
            const packaged = await packagedUpdater.downloadAndInstall();
            if (packaged.ok) {
              emit({
                stage: "install",
                message: "Installing app shell update — Marionette will relaunch",
                percent: 100,
              });
              return {
                ok: true,
                installerUpdateRequired: true,
                packagedInstallPending: true,
                sourceUpdated: !sourceResult.skipped,
                note: sourceResult.skipped
                  ? "Installing the packaged Marionette shell update."
                  : "Checkout and app shell update applied in one restart.",
                ...shellVerdict,
              };
            }
            appendUpdateLog(`[packaged] seamless install failed: ${packaged.error || "unknown"}`);
          } catch (err) {
            appendUpdateLog(`[packaged] seamless install error: ${err && err.message ? err.message : err}`);
          }
          emit({
            stage: "error",
            message:
              shellVerdict.note ||
              "Could not finish the app shell installer. Download the latest release to complete the update.",
          });
          return {
            ok: false,
            code: "installer_required",
            sourceUpdated: !sourceResult.skipped,
            installerUpdateRequired: true,
            error:
              shellVerdict.note ||
              "Install the latest Marionette release to finish the app shell update.",
            ...shellVerdict,
          };
        }
        // Packaged updater disabled/unavailable but shell still owed.
        return {
          ok: false,
          code: "installer_required",
          sourceUpdated: !sourceResult.skipped,
          installerUpdateRequired: true,
          error:
            shellVerdict.note ||
            "Install the latest Marionette release to finish the app shell update.",
          ...shellVerdict,
        };
      }

      if (!sourceResult.skipped && sourceResult.ok) {
        if (shouldRelaunchAfterSourceUpdate({
          ok: true,
          isPackaged,
          installerUpdateRequired: false,
          packagedInstallPending: false,
        })) {
          setTimeout(() => { try { relaunch(); } catch { void 0; } }, 400);
        }
        return sourceResult;
      }

      return { ok: false, error: "no update available" };
    } finally {
      applying = false;
    }
  });

  // Open the repo (or its commits) in the default browser.
  ipcMain.handle("updates:openRepo", async (_e, sub) => {
    const target = sub === "commits" ? `${REPO_HTML_URL}/commits/${DEFAULT_BRANCH}` : REPO_HTML_URL;
    try { await shell.openExternal(target); return true; } catch { return false; }
  });

  // Releases page for manual installer download when packaged feed cannot run.
  ipcMain.handle("updates:openReleases", async () => {
    const target = `${REPO_HTML_URL}/releases/latest`;
    try { await shell.openExternal(target); return true; } catch { return false; }
  });
}

module.exports = {
  registerUpdateBridge,
  checkForUpdate,
  applyUpdate,
  isTrackedSelfEditLine,
  statusPath,
  isUnmergedStatusLine,
  mergeFailureLooksLikeStaleIndex,
  parseOrphanedAutoUpdateStashRefs,
  recoverOrphanedAutoUpdateStashes,
  isElectronMainProcessFile,
  updateChangesElectronMain,
  describeMainProcessUpdate,
  resolvePuppetmasterPin,
};
