const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const {
  putSecret,
  presenceOf,
  injectEnv,
  redactText,
  vaultPath,
} = require("./secret-vault.cjs");

function tmpDir() {
  return fs.mkdtempSync(path.join(os.tmpdir(), "sv-"));
}

test("putSecret encrypts and presence hides the value", () => {
  const stateDir = tmpDir();
  const fakeSafe = {
    isEncryptionAvailable: () => true,
    encryptString: (s) => Buffer.from(`enc:${s}`),
    decryptString: (b) => Buffer.from(b).toString("utf8").slice(4),
  };
  const saved = putSecret({
    stateDir,
    safeStorage: fakeSafe,
    agentId: "sess-a",
    connector: "pypi",
    field: "token",
    value: "pypi-electron-token-xyz",
  });
  assert.equal(saved.present, true);
  const raw = fs.readFileSync(vaultPath(stateDir), "utf8");
  assert.equal(raw.includes("pypi-electron-token-xyz"), false);
  const row = presenceOf({ stateDir, agentId: "sess-a", connector: "pypi", field: "token" });
  assert.equal(row.present, true);
  assert.equal(row.state, "present");
  assert.equal(JSON.stringify(row).includes("pypi-electron-token"), false);
});

test("injectEnv maps pypi token for twine without listing the value API", () => {
  const stateDir = tmpDir();
  const fakeSafe = {
    isEncryptionAvailable: () => false,
  };
  putSecret({
    stateDir,
    safeStorage: fakeSafe,
    agentId: "sess-a",
    connector: "pypi",
    field: "token",
    value: "pypi-electron-token-xyz",
  });
  const env = injectEnv({ stateDir, safeStorage: fakeSafe, agentId: "sess-a", target: {} });
  assert.equal(env.TWINE_USERNAME, "__token__");
  assert.equal(env.TWINE_PASSWORD, "pypi-electron-token-xyz");
  const listed = presenceOf({ stateDir, agentId: "sess-a" });
  assert.equal(listed.connectors.pypi.token, "present");
  assert.equal(JSON.stringify(listed).includes("pypi-electron-token-xyz"), false);
});

test("redactText covers process-list artifacts", () => {
  const out = redactText("TWINE_PASSWORD=pypi-electron-token-xyz");
  assert.equal(out.includes("pypi-electron-token-xyz"), false);
  assert.match(out, /REDACTED/);
});
