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

test("protection is os only with a real OS key store", () => {
  const { protectionOf } = require("./secret-vault.cjs");
  const avail = (backend) => ({
    isEncryptionAvailable: () => true,
    getSelectedStorageBackend: () => backend,
  });
  assert.equal(protectionOf(avail("unknown"), "darwin"), "os");
  assert.equal(protectionOf(avail("unknown"), "win32"), "os");
  assert.equal(protectionOf(avail("gnome_libsecret"), "linux"), "os");
  assert.equal(protectionOf(avail("kwallet6"), "linux"), "os");
  assert.equal(protectionOf(avail("basic_text"), "linux"), "none");
  assert.equal(protectionOf({ isEncryptionAvailable: () => false }, "darwin"), "none");
  assert.equal(protectionOf(null, "darwin"), "none");
});

test("a value without OS protection is labeled, not reported as encrypted", () => {
  const stateDir = tmpDir();
  const saved = putSecret({
    stateDir,
    safeStorage: { isEncryptionAvailable: () => false },
    agentId: "default",
    connector: "pypi",
    field: "token",
    value: "pypi-plain-token",
  });
  assert.equal(saved.protection, "none");
  const blob = JSON.parse(fs.readFileSync(vaultPath(stateDir), "utf8")).agents.default.pypi.token;
  assert.equal(blob.alg, "unprotected");
  const row = presenceOf({ stateDir, agentId: "default", connector: "pypi", field: "token" });
  assert.equal(row.protection, "none");
  const env = injectEnv({ stateDir, safeStorage: null, agentId: "default", target: {} });
  assert.equal(env.TWINE_PASSWORD, "pypi-plain-token");
});

test("legacy b64 values still decrypt", () => {
  const stateDir = tmpDir();
  fs.writeFileSync(vaultPath(stateDir), JSON.stringify({ v: 1, agents: { default: { pypi: { token: {
    alg: "b64", data: Buffer.from("legacy-token-value").toString("base64") } } } } }));
  const env = injectEnv({ stateDir, safeStorage: null, agentId: "default", target: {} });
  assert.equal(env.TWINE_PASSWORD, "legacy-token-value");
  assert.equal(presenceOf({ stateDir, agentId: "default", connector: "pypi", field: "token" }).protection, "none");
});

test("redactText covers process-list artifacts", () => {
  const out = redactText("TWINE_PASSWORD=pypi-electron-token-xyz");
  assert.equal(out.includes("pypi-electron-token-xyz"), false);
  assert.match(out, /REDACTED/);
});
