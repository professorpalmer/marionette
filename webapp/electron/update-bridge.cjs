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
const { planPuppetmasterUpgrade, DEFAULT_PUPPETMASTER_SPEC } = require("./update-pm.cjs");
const marker = require("./update-marker.cjs");

const DEFAULT_BRANCH = process.env.PMHARNESS_UPDATE_BRANCH || "main";
const REPO_HTML_URL = "https://github.com/professorpalmer/marionette";

function pmharnessHome() {
  return path.join(os.homedir(), ".pmharness");
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

function mergeFailureLooksLikeStaleIndex(text) {
  return /could not write index|needs merge|unmerged files|you have not concluded your merge|merge_head/i.test(text || "");
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
    const beforeSha = (await gitCapture(repoRoot, ["rev-parse", "HEAD"])).out;

    // fetch
    progress("fetch", "Fetching latest changes", 0);
    const origin = await gitCapture(repoRoot, ["config", "--get", "remote.origin.url"]);
    if (!origin.ok) return { ok: false, error: "not a git checkout (no origin remote)" };
    const fetched = await runStreamed("git", ["-C", repoRoot, "fetch", "--no-tags", "origin", branch], { env: childEnv },
      (l) => progress("fetch", l, 0.5));
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
      (l) => progress("pull", l, 0.5));
    if (pulled.code !== 0 && mergeFailureLooksLikeStaleIndex(pulled.tail)) {
      progress("pull", "Repairing stale update state", 0.55);
      const repaired = await recoverInterruptedMerge(repoRoot);
      if (repaired.ok) {
        pulled = await runStreamed("git", ["-C", repoRoot, "merge", "--ff-only", "FETCH_HEAD"], { env: childEnv },
          (l) => progress("pull", l, 0.65));
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

    const py = process.env.PMHARNESS_PYTHON || (process.platform === "win32"
      ? path.join(repoRoot, ".venv", "Scripts", "python.exe")
      : path.join(repoRoot, ".venv", "bin", "python"));
    // Marionette venvs are created by `uv venv`, which does NOT install pip, so
    // prefer `uv pip ...`. Fall back to `python -m pip` for an older pip-bearing
    // venv. Detected once and reused for both the app and the Puppetmaster step.
    const hasUv = await detectUv(childEnv);

    if (pyChanged) {
      progress("deps", "Updating Python dependencies", 0.3);
      const dep = hasUv
        ? await runStreamed("uv", ["pip", "install", "--python", py, "-e", "."],
            { cwd: repoRoot, env: childEnv }, (l) => progress("deps", l, 0.4))
        : await runStreamed(py, ["-m", "pip", "install", "-e", ".", "--quiet"],
            { cwd: repoRoot, env: childEnv }, (l) => progress("deps", l, 0.4));
      if (dep.code !== 0) return { ok: false, error: dep.tail || "python dependency install failed" };
    }
    if (nodeChanged) {
      progress("deps", "Updating node dependencies", 0.7);
      const npmci = await runNpmStreamed(["ci"], { cwd: path.join(repoRoot, "webapp"), env: childEnv },
        (l) => progress("deps", l, 0.8));
      if (npmci.code !== 0) return { ok: false, error: npmci.tail || "npm ci failed" };
    }

    // Puppetmaster -- the one integral runtime dep -- ships independently of this
    // repo (unpinned PyPI package), so a git pull never carries a PM release.
    // Upgrade it on every update so overhauls reach existing installs, unless a
    // dev/custom spec owns it. Non-fatal: a PyPI blip or offline machine must
    // never strand an otherwise-successful app update -- PM just stays put.
    progress("deps", "Checking Puppetmaster", 0.85);
    const pmShow = hasUv
      ? await execCapture("uv", ["pip", "show", "--python", py, DEFAULT_PUPPETMASTER_SPEC], { env: childEnv })
      : await execCapture(py, ["-m", "pip", "show", DEFAULT_PUPPETMASTER_SPEC], { env: childEnv });
    const pmPlan = planPuppetmasterUpgrade({
      specEnv: process.env.MARIONETTE_PUPPETMASTER_SPEC,
      pipShowOutput: pmShow.out,
    });
    if (pmPlan.skip) {
      progress("deps", "Puppetmaster: " + pmPlan.reason + ", leaving as-is", 0.9);
    } else {
      progress("deps", "Updating Puppetmaster", 0.9);
      const pm = hasUv
        ? await runStreamed("uv", ["pip", "install", "--python", py, "--upgrade", pmPlan.spec],
            { cwd: repoRoot, env: childEnv }, (l) => progress("deps", l, 0.92))
        : await runStreamed(py, ["-m", "pip", "install", "--upgrade", pmPlan.spec, "--quiet"],
            { cwd: repoRoot, env: childEnv }, (l) => progress("deps", l, 0.92));
      if (pm.code !== 0) {
        progress("deps", "Puppetmaster upgrade skipped: " + (pm.tail || "unavailable"), 0.95);
      }
    }

    // build -- rebuild the renderer into dist/. Retry once: a first build can
    // trip on a still-settling tree; the second is a near-no-op if the first won.
    const rebuild = async (attempt) => {
      progress("build", attempt === 0 ? "Rebuilding app" : "Rebuilding app (retry)", 0.1);
      return runNpmStreamed(["run", "build"], { cwd: path.join(repoRoot, "webapp"), env: childEnv },
        (l) => progress("build", l, 0.5));
    };
    const built = await runRebuildWithRetry(rebuild);
    if (built.code !== 0) return { ok: false, error: built.tail || "renderer build failed" };

    progress("done", "Update ready -- relaunching", 1);
    return { ok: true };
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
// tears down the backend and re-execs the app.
function registerUpdateBridge(ipcMain, app, shell, opts = {}) {
  const getRepoRoot = opts.getRepoRoot || (() => path.join(os.homedir(), "pm-harness"));
  const relaunch = opts.relaunch || (() => { app.relaunch(); app.exit(0); });
  // Login-shell-augmented env for the updater's child processes (git/npm/uv), so
  // a Finder/Dock launch with a stripped launchd PATH can still find them. Omit
  // to inherit process.env (dev/CLI runs already have a full env).
  const getEnv = opts.getEnv || (() => process.env);
  let applying = false;

  ipcMain.handle("updates:check", async () => {
    const currentVersion = app.getVersion();
    const res = await checkForUpdate({ repoRoot: getRepoRoot(), currentVersion, env: getEnv() });
    return { current: currentVersion, ...res };
  });

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
    try {
      const result = await applyUpdate({ repoRoot: getRepoRoot(), strategy, env: getEnv() }, emit);
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

module.exports = {
  registerUpdateBridge,
  checkForUpdate,
  applyUpdate,
  isTrackedSelfEditLine,
  statusPath,
  isUnmergedStatusLine,
  mergeFailureLooksLikeStaleIndex,
};
