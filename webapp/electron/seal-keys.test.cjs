const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const seal = require("./seal-keys.cjs");

function tmpDir() {
  return fs.mkdtempSync(path.join(os.tmpdir(), "seal-"));
}

function fakeSafe({ available = true, backend = "keychain" } = {}) {
  return {
    isEncryptionAvailable: () => available,
    getSelectedStorageBackend: () => backend,
    encryptString: (s) => Buffer.from(`os:${s}`),
    decryptString: (b) => Buffer.from(b).toString("utf8").slice(3),
  };
}

test("key id matches harness/seal.py key_id", () => {
  // Python: seal.key_id(bytes(range(32))).hex()
  assert.equal(seal.keyId(Buffer.from([...Array(32).keys()])), "6460fb87eee866bd");
});

test("enable creates the key once and writes config last", () => {
  const stateDir = tmpDir();
  const safeStorage = fakeSafe();
  const first = seal.enable({ stateDir, safeStorage, platform: "darwin" });
  assert.equal(first.masterKey.length, 32);
  assert.match(first.recoveryCode, /^([A-HJ-NP-Z2-9]{4}-){5}[A-HJ-NP-Z2-9]{4}$/);
  const config = seal.readConfig(stateDir);
  assert.equal(config.enabled, true);
  assert.equal(config.key_id, seal.keyId(first.masterKey));
  const paths = seal.sealPaths(stateDir);
  const onDisk = Buffer.concat([fs.readFileSync(paths.recovery), fs.readFileSync(paths.config)]);
  assert.equal(onDisk.includes(first.masterKey), false);
  assert.equal(onDisk.includes(first.masterKey.toString("base64")), false);

  seal.disable({ stateDir });
  assert.equal(seal.readConfig(stateDir).enabled, false);
  const again = seal.enable({ stateDir, safeStorage, platform: "darwin" });
  assert.equal(again.recoveryCode, null);
  assert.deepEqual(again.masterKey, first.masterKey);
});

test("no OS key store refuses enable and gives no spawn key", () => {
  const stateDir = tmpDir();
  for (const safeStorage of [fakeSafe({ available: false }), fakeSafe({ backend: "basic_text" })]) {
    assert.throws(() => seal.enable({ stateDir, safeStorage, platform: "linux" }),
      { code: "SEAL_NO_OS_PROTECTION" });
  }
  assert.equal(seal.spawnLine({ stateDir, safeStorage: fakeSafe(), platform: "darwin" }), null);
});

test("spawn line carries the key, or an empty object when the key store lost it", () => {
  const stateDir = tmpDir();
  const safeStorage = fakeSafe();
  const { masterKey } = seal.enable({ stateDir, safeStorage, platform: "darwin" });
  const line = seal.spawnLine({ stateDir, safeStorage, platform: "darwin" });
  assert.ok(line.endsWith("\n"));
  assert.equal(JSON.parse(line).master_key, masterKey.toString("base64"));
  fs.rmSync(seal.sealPaths(stateDir).os);
  assert.equal(seal.spawnLine({ stateDir, safeStorage, platform: "darwin" }), "{}\n");
  assert.throws(() => seal.enable({ stateDir, safeStorage, platform: "darwin" }),
    { code: "SEAL_KEY_UNAVAILABLE" });
});

test("recovery code restores the key into the key store", () => {
  const stateDir = tmpDir();
  const safeStorage = fakeSafe();
  const { masterKey, recoveryCode } = seal.enable({ stateDir, safeStorage, platform: "darwin" });
  fs.rmSync(seal.sealPaths(stateDir).os);
  assert.throws(() => seal.recover({ stateDir, safeStorage, platform: "darwin", code: "AAAA-AAAA" }),
    { code: "SEAL_RECOVERY_REJECTED" });
  const loose = ` ${recoveryCode.toLowerCase().replace(/-/g, " ")} `;
  assert.deepEqual(seal.recover({ stateDir, safeStorage, platform: "darwin", code: loose }), masterKey);
  assert.deepEqual(seal.loadMasterKey({ stateDir, safeStorage, platform: "darwin" }), masterKey);
});

test("a key that does not match config.json is never used", () => {
  const stateDir = tmpDir();
  const safeStorage = fakeSafe();
  seal.enable({ stateDir, safeStorage, platform: "darwin" });
  const other = Buffer.alloc(32, 7);
  fs.writeFileSync(seal.sealPaths(stateDir).os, safeStorage.encryptString(other.toString("base64")));
  assert.equal(seal.loadMasterKey({ stateDir, safeStorage, platform: "darwin" }), null);
  assert.equal(seal.status({ stateDir, safeStorage, platform: "darwin" }).keyAvailable, false);
});

test("forget removes config first so a half removal is not configured", () => {
  const stateDir = tmpDir();
  seal.enable({ stateDir, safeStorage: fakeSafe(), platform: "darwin" });
  seal.forget({ stateDir });
  assert.equal(seal.readConfig(stateDir), null);
  assert.deepEqual(fs.readdirSync(seal.sealPaths(stateDir).dir), []);
});
