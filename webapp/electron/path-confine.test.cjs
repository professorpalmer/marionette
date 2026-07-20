const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { describe, it } = require("node:test");
const {
  pathWithin,
  denyOutsideAllowedRoots,
  loadWorkspaceAllowedRoots,
} = require("./path-confine.cjs");

describe("path-confine", () => {
  it("pathWithin accepts nested and equal paths", () => {
    const root = path.join(os.tmpdir(), "pmh-confine-root");
    assert.equal(pathWithin(root, root, { allow_equal: true }), true);
    assert.equal(
      pathWithin(path.join(root, "a", "b.py"), root, { allow_equal: true }),
      true,
    );
    assert.equal(
      pathWithin(path.join(root, "..", "escape"), root, { allow_equal: true }),
      false,
    );
  });

  it("denyOutsideAllowedRoots blocks traversal and foreign homes", () => {
    const root = path.resolve(path.join(os.tmpdir(), "pmh-ws-allowed"));
    assert.equal(denyOutsideAllowedRoots(path.join(root, "src"), [root]), null);
    assert.match(
      denyOutsideAllowedRoots(path.join(root, "..", "secrets"), [root]) || "",
      /not allowed/,
    );
    assert.match(
      denyOutsideAllowedRoots(path.join(os.homedir(), ".ssh", "id_rsa"), [root]) || "",
      /not allowed/,
    );
    assert.match(denyOutsideAllowedRoots(root, []) || "", /no workspace/);
  });

  it("loadWorkspaceAllowedRoots reads repo + recents", () => {
    const roots = loadWorkspaceAllowedRoots((name) => {
      assert.equal(name, "workspace.json");
      return JSON.stringify({
        repo: "C:/Users/pwall/Projects/marionette",
        recents: ["C:/Users/pwall/Projects/Puppetmaster", ""],
      });
    });
    assert.equal(roots.length, 2);
    assert.ok(roots.some((r) => /marionette/i.test(r)));
    assert.ok(roots.some((r) => /Puppetmaster/i.test(r)));
  });
});

describe("fs-bridge path confinement wiring", () => {
  it("registers guards that reject paths outside injected roots", async () => {
    // Plain node --test has no Electron binary; stub the shell import.
    const electronPath = require.resolve("electron");
    require.cache[electronPath] = {
      id: electronPath,
      filename: electronPath,
      loaded: true,
      exports: { shell: { showItemInFolder() {} } },
    };
    delete require.cache[require.resolve("./fs-bridge.cjs")];
    const { registerFsBridge } = require("./fs-bridge.cjs");
    const handlers = new Map();
    const ipcMain = {
      handle(channel, fn) {
        handlers.set(channel, fn);
      },
    };
    const root = fs.mkdtempSync(path.join(os.tmpdir(), "pmh-fs-ok-"));
    const secret = path.join(os.tmpdir(), "pmh-fs-secret.txt");
    fs.writeFileSync(secret, "top-secret", "utf8");
    fs.writeFileSync(path.join(root, "ok.txt"), "safe", "utf8");
    try {
      registerFsBridge(ipcMain, { getAllowedRoots: () => [root] });
      const readFile = handlers.get("fs:readFile");
      assert.ok(readFile);
      const ok = await readFile(null, path.join(root, "ok.txt"));
      assert.equal(ok.ok, true);
      assert.equal(ok.content, "safe");
      const denied = await readFile(null, secret);
      assert.equal(denied.ok, false);
      assert.match(denied.error, /not allowed/);
      const readDir = handlers.get("fs:readDir");
      const dirDenied = await readDir(null, path.dirname(secret));
      assert.equal(dirDenied.ok, false);
      assert.match(dirDenied.error, /not allowed/);
    } finally {
      try { fs.rmSync(root, { recursive: true, force: true }); } catch { /* */ }
      try { fs.unlinkSync(secret); } catch { /* */ }
    }
  });
});

describe("git:applyHunk binary write", () => {
  it("writes patch temp file as Buffer (no utf8 text mode)", () => {
    const src = fs.readFileSync(path.join(__dirname, "git-bridge.cjs"), "utf8");
    assert.match(src, /Buffer\.from\(String\(patchText/);
    assert.doesNotMatch(src, /writeFile\(tmpfile,\s*patchText,\s*"utf8"\)/);
  });

  it("guards git handlers against foreign repo paths", async () => {
    const { registerGitBridge } = require("./git-bridge.cjs");
    const handlers = new Map();
    const ipcMain = {
      handle(channel, fn) {
        handlers.set(channel, fn);
      },
    };
    const root = fs.mkdtempSync(path.join(os.tmpdir(), "pmh-git-ok-"));
    try {
      registerGitBridge(ipcMain, { getAllowedRoots: () => [root] });
      const status = handlers.get("git:status");
      const denied = await status(null, path.join(os.homedir(), ".ssh"));
      assert.equal(denied.ok, false);
      assert.match(denied.error, /not allowed/);
    } finally {
      try { fs.rmSync(root, { recursive: true, force: true }); } catch { /* */ }
    }
  });
});

describe("main does not inject renderer token global", () => {
  it("executeJavaScript only sets __HARNESS_PORT__", () => {
    const src = fs.readFileSync(path.join(__dirname, "main.cjs"), "utf8");
    assert.match(src, /window\.__HARNESS_PORT__=\$\{backendPort\}/);
    assert.doesNotMatch(
      src,
      /window\.__HARNESS_TOKEN__=\$\{JSON\.stringify\(harnessToken\)\}/,
    );
  });
});
