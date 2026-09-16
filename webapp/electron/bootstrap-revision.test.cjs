"use strict";
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const vm = require("node:vm");
const { execFileSync } = require("node:child_process");
const git = (dir, ...args) => execFileSync("git", ["-C", dir, ...args], { encoding: "utf8" }).trim();
function fixture(t, packaged = false) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "bootstrap-revision-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const origin = path.join(root, "origin"); fs.mkdirSync(origin);
  git(origin, "init", "-b", "main"); git(origin, "config", "user.email", "test@example.invalid"); git(origin, "config", "user.name", "Test");
  fs.mkdirSync(path.join(origin, "webapp"));
  fs.writeFileSync(path.join(origin, ".gitignore"), ".venv/\nwebapp/node_modules/\nwebapp/dist/\n");
  fs.writeFileSync(path.join(origin, "webapp/package-lock.json"), '{"version":1}');
  fs.writeFileSync(path.join(origin, "webapp/package.json"), '{}');
  fs.writeFileSync(path.join(origin, "pyproject.toml"), "[project]\nname='test'\n");
  git(origin, "add", "."); git(origin, "commit", "-qm", "first");
  const revision = git(origin, "rev-parse", "HEAD");
  fs.writeFileSync(path.join(origin, "webapp/package-lock.json"), '{"version":2}');
  git(origin, "commit", "-qam", "second");
  const dest = path.join(root, "checkout");
  const packagedDir = path.join(root, "packaged"); fs.mkdirSync(packagedDir);
  const calls = []; let failBuild = false;
  const context = { require, module: { exports: {} }, __dirname: packagedDir, process: { ...process, env: { ...process.env, MARIONETTE_REPO_URL: origin, MARIONETTE_REVISION: revision } }, setImmediate, setTimeout, console,
    provision: async (dir) => { const py = process.platform === "win32" ? ".venv/Scripts/python.exe" : ".venv/bin/python"; fs.mkdirSync(path.dirname(path.join(dir, py)), { recursive: true }); fs.writeFileSync(path.join(dir, py), "python"); },
    npm: async (args, opts) => { calls.push(args.join(" ")); if (args[0] === "ci") fs.mkdirSync(path.join(opts.cwd, "node_modules"), { recursive: true }); else { if (failBuild) throw Error("interrupted build"); fs.mkdirSync(path.join(opts.cwd, "dist"), { recursive: true }); fs.writeFileSync(path.join(opts.cwd, "dist/index.html"), "built"); } },
  };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, "bootstrap.cjs"), "utf8") + '\nhydratePath = () => {}; ensurePortableGit = ensureUv = ensurePortableNode = async () => {}; provisionPython = provision; runNpmAsync = npm;', context);
  if (packaged) vm.runInNewContext(`bootstrapTarget = () => (${JSON.stringify({ mode: "packaged", repo: origin, revision })});`, context);
  return { origin, dest, revision, calls, packagedDir, api: context.module.exports,
    retarget: sha => vm.runInNewContext(`bootstrapTarget = () => (${JSON.stringify({ mode: "packaged", repo: origin, revision: sha })});`, context), fail: () => { failBuild = true; }, recover: () => { failBuild = false; } };
}
test("bootstrap pins the requested commit even when main has advanced", async t => {
  const f = fixture(t); await f.api.runBootstrap(f.dest);
  assert.equal(git(f.dest, "rev-parse", "HEAD"), f.revision);
  assert.equal(f.api.isInstallComplete(f.dest), true);
});
test("an interrupted build with existing node_modules reruns npm ci", async t => {
  const f = fixture(t); f.fail(); await assert.rejects(f.api.runBootstrap(f.dest), /interrupted/);
  assert.equal(f.api.isInstallComplete(f.dest), false); f.recover(); await f.api.runBootstrap(f.dest);
  assert.equal(f.calls.filter(x => x === "ci").length, 2);
});
test("dirty checkout is refused without changing its files or HEAD", async t => {
  const f = fixture(t); await f.api.runBootstrap(f.dest);
  fs.writeFileSync(path.join(f.dest, "webapp/package-lock.json"), "user edit");
  const before = git(f.dest, "rev-parse", "HEAD");
  assert.equal(f.api.isInstallComplete(f.dest), false);
  await assert.rejects(f.api.runBootstrap(f.dest), /[Dd]irty|[Ll]ocal changes/);
  assert.equal(fs.readFileSync(path.join(f.dest, "webapp/package-lock.json"), "utf8"), "user edit");
  assert.equal(git(f.dest, "rev-parse", "HEAD"), before);
});
test("a stale lock receipt and a missing renderer force reinstall", async t => {
  const f = fixture(t); await f.api.runBootstrap(f.dest);
  git(f.dest, "fetch", "origin", "main");
  git(f.dest, "checkout", "--detach", git(f.origin, "rev-parse", "HEAD"));
  assert.equal(f.api.isInstallComplete(f.dest), false);
  await f.api.runBootstrap(f.dest);
  assert.equal(git(f.dest, "rev-parse", "HEAD"), f.revision);
  fs.unlinkSync(path.join(f.dest, "webapp/dist/index.html"));
  assert.equal(f.api.isInstallComplete(f.dest), false);
  await f.api.runBootstrap(f.dest);
  assert.equal(f.calls.filter(x => x === "ci").length, 3);
});

test("completion is reused only while checkout and dependencies match", async t => {
  const f = fixture(t); await f.api.runBootstrap(f.dest); await f.api.runBootstrap(f.dest);
  assert.deepEqual(f.calls, ["ci", "run build"]);
  fs.rmSync(path.join(f.dest, "webapp/node_modules"), { recursive: true });
  assert.equal(f.api.isInstallComplete(f.dest), false);
  await f.api.runBootstrap(f.dest);
  assert.equal(f.calls.filter(x => x === "ci").length, 2);
});

test("wrong origin and untracked user files are preserved and refused", async t => {
  const f = fixture(t); await f.api.runBootstrap(f.dest);
  fs.writeFileSync(path.join(f.dest, "user.txt"), "keep");
  await assert.rejects(f.api.runBootstrap(f.dest), /Local changes/);
  fs.renameSync(path.join(f.dest, "user.txt"), path.join(f.origin, "saved.txt"));
  git(f.dest, "remote", "set-url", "origin", f.origin + "-other");
  await assert.rejects(f.api.runBootstrap(f.dest), /origin differs/);
  assert.equal(fs.readFileSync(path.join(f.origin, "saved.txt"), "utf8"), "keep");
});

test("packaging hook embeds actual HEAD through the installed builder callback", async t => {
  const f = fixture(t);
  const appDir = path.join(f.origin, "webapp"); fs.mkdirSync(path.join(appDir, "electron"));
  fs.appendFileSync(path.join(f.origin, ".gitignore"), "webapp/electron/bootstrap-revision.json\n");
  fs.writeFileSync(path.join(appDir, "package.json"), '{"version":"99.0.0"}');
  git(f.origin, "add", "."); git(f.origin, "commit", "-qm", "packaging");
  const hook = require("./before-pack.cjs");
  const { PlatformPackager } = require("app-builder-lib");
  const stopAfterHook = new Error("stop before dependency installation");
  let metadata;
  const packager = {
    packagerOptions: {},
    info: {
      appDir,
      cancellationToken: { cancelled: false },
      async emitBeforePack(context) { metadata = await hook(context); },
      installAppDependencies() { throw stopAfterHook; },
    },
  };
  await assert.rejects(
    PlatformPackager.prototype.doPack.call(packager, {}),
    error => error === stopAfterHook,
  );
  assert.equal(metadata.revision, git(f.origin, "rev-parse", "HEAD"));
  assert.equal(metadata.tree, git(f.origin, "rev-parse", "HEAD^{tree}"));
  assert.equal(metadata.version, "99.0.0");
  assert.deepEqual(await hook({ packager }), metadata);
  fs.writeFileSync(path.join(appDir, "package.json"), '{"version":"dirty"}');
  await assert.rejects(hook({ packager }), /dirty checkout/);
  const config = fs.readFileSync(path.join(__dirname, "../electron-builder.yml"), "utf8");
  assert.match(config, /beforePack: .\/electron\/before-pack.cjs/);
  assert.match(config, /electron\/\*\*\/\*/);
});

test("missing release metadata fails closed and overrides are explicitly development", t => {
  const { api } = fixture(t);
  assert.throws(() => api.bootstrapTarget({}), /source revision is missing/);
  assert.equal(api.bootstrapTarget({ MARIONETTE_BRANCH: "dev" }).mode, "development");
  assert.throws(() => api.bootstrapTarget({ MARIONETTE_REVISION: "main" }), /full Git commit SHA/);
});

test("an interrupted initial fetch resumes from an unborn checkout", async t => {
  const f = fixture(t); fs.mkdirSync(f.dest); git(f.dest, "init");
  git(f.dest, "remote", "add", "origin", f.origin);
  await f.api.runBootstrap(f.dest);
  assert.equal(git(f.dest, "rev-parse", "HEAD"), f.revision);
  assert.equal(f.api.isInstallComplete(f.dest), true);
});

test("packaged defaults load embedded SHA without looking up a version tag", t => {
  const f = fixture(t);
  fs.writeFileSync(path.join(f.packagedDir, "bootstrap-revision.json"), JSON.stringify({
    schema: 1, repo: "https://github.com/professorpalmer/marionette.git", revision: f.revision, version: "99.0.0",
  }));
  const target = f.api.bootstrapTarget({});
  assert.equal(target.mode, "packaged"); assert.equal(target.revision, f.revision);
  assert.equal(f.api.bootstrapTarget({ MARIONETTE_REVISION: f.revision }).mode, "development");
  fs.writeFileSync(path.join(f.packagedDir, "bootstrap-revision.json"), '{"schema":1,"revision":"main"}');
  assert.throws(() => f.api.bootstrapTarget({}), /Invalid packaged source revision/);
});

test("failed rebuild cannot reuse old dist or a previous success receipt", async t => {
  const f = fixture(t); await f.api.runBootstrap(f.dest);
  const receipt = path.join(f.dest, ".git", "marionette-bootstrap.json");
  const stale = JSON.parse(fs.readFileSync(receipt)); stale.inputs = "obsolete lock";
  fs.writeFileSync(receipt, JSON.stringify(stale));
  assert.equal(f.api.isInstallComplete(f.dest), false);
  f.fail(); await assert.rejects(f.api.runBootstrap(f.dest), /interrupted build/);
  assert.equal(fs.existsSync(path.join(f.dest, "webapp/dist/index.html")), true);
  assert.equal(f.api.isInstallComplete(f.dest), false);
  f.recover(); await f.api.runBootstrap(f.dest);
  assert.equal(f.calls.filter(x => x === "ci").length, 3);
  assert.equal(f.api.isInstallComplete(f.dest), true);
});

for (const dirty of ['untracked', 'tracked', 'origin']) {
  test(`production selection preserves legacy ${dirty} and reuses one pinned checkout`, async t => {
    const f = fixture(t, true);
    const home = path.dirname(f.dest);
    const legacy = path.join(home, '.marionette', 'marionette');
    await f.api.runBootstrap(legacy);
    const userFile = path.join(legacy, dirty === 'tracked' ? 'pyproject.toml' : 'tests/test_stream_chat_framing_done.py');
    fs.mkdirSync(path.dirname(userFile), { recursive: true });
    fs.writeFileSync(userFile, 'user work\n\x00exact bytes');
    if (dirty === 'origin') git(legacy, 'remote', 'set-url', 'origin', f.origin + '-unrelated');
    const hash = file => require('node:crypto').createHash('sha256').update(fs.readFileSync(file)).digest('hex');
    const before = hash(userFile), head = git(legacy, 'rev-parse', 'HEAD');
    const selected = f.api.selectPackagedCheckout({ home, env: {} });
    assert.notEqual(selected, legacy);
    f.fail(); await assert.rejects(f.api.runBootstrap(selected), /interrupted build/);
    assert.equal(f.api.isInstallComplete(selected), false);
    f.recover(); await f.api.runBootstrap(selected);
    const calls = f.calls.length;
    assert.equal(f.api.selectPackagedCheckout({ home, env: {} }), selected);
    await f.api.runBootstrap(selected);
    assert.equal(f.calls.length, calls);
    assert.equal(git(selected, 'rev-parse', 'HEAD'), f.revision);
    assert.equal(hash(userFile), before);
    assert.equal(git(legacy, 'rev-parse', 'HEAD'), head);
    assert.equal(fs.readdirSync(path.dirname(selected)).length, 2);
  });
}

test('explicit development choices retain their checkout contract', () => {
  const { selectPackagedCheckout } = require('./bootstrap.cjs');
  const home = path.resolve(os.tmpdir(), 'selection-only');
  for (const key of ['MARIONETTE_CHECKOUT', 'HARNESS_CHECKOUT']) {
    assert.equal(selectPackagedCheckout({ home, env: { [key]: '/chosen' } }), '/chosen');
  }
  for (const env of [{ MARIONETTE_BRANCH: 'dev' }, { MARIONETTE_REVISION: 'a'.repeat(40) },
    { MARIONETTE_REPO_URL: '/fork' }, { MARIONETTE_SELF_DEV: 'true' }, { PMHARNESS_DEV_SERVER: 'http://localhost:5273' }]) {
    assert.equal(selectPackagedCheckout({ home, env }), path.join(home, '.marionette', 'marionette'));
  }
  assert.equal(selectPackagedCheckout({ home, env: { MARIONETTE_SELF_DEV: 'false' } }),
    selectPackagedCheckout({ home, env: {} }));
});


test('persisted self-dev honors the legacy root and records the current root for later toggles', () => {
  const { selectPackagedCheckout } = require('./bootstrap.cjs');
  const home = path.resolve(os.tmpdir(), 'persisted-self-dev');
  const release = selectPackagedCheckout({ home, env: {} });
  assert.equal(selectPackagedCheckout({ home, env: {}, selfDev: true }), path.join(home, '.marionette', 'marionette'));
  assert.equal(selectPackagedCheckout({ home, env: {}, selfDev: true, selfDevCheckout: release }), release);
  assert.equal(selectPackagedCheckout({ home, env: { HARNESS_CHECKOUT: '/explicit' }, selfDev: true, selfDevCheckout: release }), '/explicit');
});


test('a replacement installer rebuilds the same production slot at its new pinned revision', async t => {
  const f = fixture(t, true);
  const selected = f.api.selectPackagedCheckout({ home: path.dirname(f.dest), env: {} });
  await f.api.runBootstrap(selected);
  const next = git(f.origin, 'rev-parse', 'HEAD');
  f.retarget(next);
  assert.equal(f.api.isInstallComplete(selected), false);
  await f.api.runBootstrap(selected);
  assert.equal(git(selected, 'rev-parse', 'HEAD'), next);
  await f.api.runBootstrap(selected);
  assert.equal(f.calls.filter(x => x === 'ci').length, 2);
  assert.deepEqual(fs.readdirSync(path.dirname(selected)), ['release']);
  fs.writeFileSync(path.join(selected, 'user-work.txt'), 'preserve even in release slot');
  f.retarget(f.revision);
  await assert.rejects(f.api.runBootstrap(selected), /Local changes/);
  assert.equal(fs.readFileSync(path.join(selected, 'user-work.txt'), 'utf8'), 'preserve even in release slot');
  assert.equal(git(selected, 'rev-parse', 'HEAD'), next);
});
