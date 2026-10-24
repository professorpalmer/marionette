/**
 * Master key custody for encrypted sessions (harness/seal.py).
 *
 * The main process owns the 32-byte master key. Two copies exist on disk,
 * both wrapped:
 *   seal/master.os              safeStorage (Keychain, DPAPI, libsecret, KWallet)
 *   seal/master.recovery.json   scrypt(recovery code) + AES-256-GCM
 * seal/config.json holds the switch and the key id only. The backend gets the
 * key on stdin at spawn, or through POST /api/seal/unlock.
 */
const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");
const { protectionOf } = require("./secret-vault.cjs");

const KEY_BYTES = 32;
const KEY_ID_LABEL = "marionette-seal-key-id";
const RECOVERY_AAD = Buffer.from("marionette-seal-recovery-v1");
const SCRYPT = { N: 1 << 15, r: 8, p: 1, maxmem: 64 * 1024 * 1024 };
// 32 symbols, so one random byte masked to 5 bits has no bias. No 0/O/1/I.
const CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789";
const CODE_LENGTH = 24;

function sealPaths(stateDir) {
  const dir = path.join(stateDir, "seal");
  return {
    dir,
    config: path.join(dir, "config.json"),
    os: path.join(dir, "master.os"),
    recovery: path.join(dir, "master.recovery.json"),
  };
}

function keyId(masterKey) {
  return crypto.createHmac("sha256", masterKey).update(KEY_ID_LABEL).digest().subarray(0, 8).toString("hex");
}

function readJson(file) {
  try {
    const data = JSON.parse(fs.readFileSync(file, "utf8"));
    return data && typeof data === "object" ? data : null;
  } catch (_) {
    return null;
  }
}

function writeAtomic(file, data) {
  fs.mkdirSync(path.dirname(file), { recursive: true, mode: 0o700 });
  const tmp = `${file}.${process.pid}.${crypto.randomBytes(4).toString("hex")}.tmp`;
  const fd = fs.openSync(tmp, "w", 0o600);
  try {
    fs.writeSync(fd, data);
    fs.fsyncSync(fd);
  } finally {
    fs.closeSync(fd);
  }
  fs.renameSync(tmp, file);
}

function readConfig(stateDir) {
  const config = readJson(sealPaths(stateDir).config);
  return config && typeof config.key_id === "string" ? config : null;
}

function newRecoveryCode() {
  const bytes = crypto.randomBytes(CODE_LENGTH);
  const chars = Array.from(bytes, (b) => CODE_ALPHABET[b & 31]).join("");
  return chars.match(/.{4}/g).join("-");
}

function normalizeCode(code) {
  return String(code || "").toUpperCase().replace(/[^A-Z0-9]/g, "");
}

function recoveryKey(code, salt) {
  return crypto.scryptSync(normalizeCode(code), salt, KEY_BYTES, SCRYPT);
}

function wrapWithCode(masterKey, code) {
  const salt = crypto.randomBytes(16);
  const nonce = crypto.randomBytes(12);
  const cipher = crypto.createCipheriv("aes-256-gcm", recoveryKey(code, salt), nonce);
  cipher.setAAD(RECOVERY_AAD);
  const ct = Buffer.concat([cipher.update(masterKey), cipher.final(), cipher.getAuthTag()]);
  return {
    v: 1,
    kdf: "scrypt",
    N: SCRYPT.N,
    r: SCRYPT.r,
    p: SCRYPT.p,
    salt: salt.toString("base64"),
    nonce: nonce.toString("base64"),
    ct: ct.toString("base64"),
    key_id: keyId(masterKey),
  };
}

function unwrapWithCode(record, code) {
  if (!record || record.v !== 1 || record.kdf !== "scrypt") throw sealError("SEAL_RECOVERY_MISSING");
  const salt = Buffer.from(record.salt, "base64");
  const raw = Buffer.from(record.ct, "base64");
  try {
    const key = crypto.scryptSync(normalizeCode(code), salt, KEY_BYTES,
      { N: record.N, r: record.r, p: record.p, maxmem: SCRYPT.maxmem });
    const decipher = crypto.createDecipheriv("aes-256-gcm", key, Buffer.from(record.nonce, "base64"));
    decipher.setAAD(RECOVERY_AAD);
    decipher.setAuthTag(raw.subarray(raw.length - 16));
    return Buffer.concat([decipher.update(raw.subarray(0, raw.length - 16)), decipher.final()]);
  } catch (_) {
    throw sealError("SEAL_RECOVERY_REJECTED");
  }
}

function sealError(code) {
  const messages = {
    SEAL_NO_OS_PROTECTION: "This system has no OS key store for the encryption key.",
    SEAL_KEY_UNAVAILABLE: "The encryption key is not available. Unlock with your recovery code.",
    SEAL_RECOVERY_MISSING: "No recovery record exists for this encryption key.",
    SEAL_RECOVERY_REJECTED: "The recovery code is not correct.",
    SEAL_NOT_CONFIGURED: "Encryption is not set up.",
  };
  return Object.assign(new Error(messages[code] || code), { code });
}

function storeWithOs(stateDir, safeStorage, masterKey) {
  writeAtomic(sealPaths(stateDir).os, safeStorage.encryptString(masterKey.toString("base64")));
}

/** The master key from the OS key store, or null. Never throws. */
function loadMasterKey({ stateDir, safeStorage, platform = process.platform }) {
  const config = readConfig(stateDir);
  if (!config || protectionOf(safeStorage, platform) !== "os") return null;
  try {
    const wrapped = fs.readFileSync(sealPaths(stateDir).os);
    const key = Buffer.from(safeStorage.decryptString(wrapped), "base64");
    return key.length === KEY_BYTES && keyId(key) === config.key_id ? key : null;
  } catch (_) {
    return null;
  }
}

function status({ stateDir, safeStorage, platform = process.platform }) {
  const config = readConfig(stateDir);
  return {
    configured: Boolean(config),
    enabled: Boolean(config && config.enabled === true),
    protection: protectionOf(safeStorage, platform),
    keyAvailable: Boolean(config && loadMasterKey({ stateDir, safeStorage, platform })),
  };
}

/**
 * Turn on encryption for new sessions. The first call creates the master key
 * and returns the recovery code; the caller shows it once and never stores it.
 */
function enable({ stateDir, safeStorage, platform = process.platform, now = Date.now }) {
  if (protectionOf(safeStorage, platform) !== "os") throw sealError("SEAL_NO_OS_PROTECTION");
  const paths = sealPaths(stateDir);
  const config = readConfig(stateDir);
  if (config) {
    const masterKey = loadMasterKey({ stateDir, safeStorage, platform });
    if (!masterKey) throw sealError("SEAL_KEY_UNAVAILABLE");
    writeAtomic(paths.config, JSON.stringify({ ...config, enabled: true }));
    return { masterKey, recoveryCode: null };
  }
  const masterKey = crypto.randomBytes(KEY_BYTES);
  const recoveryCode = newRecoveryCode();
  // Both wrapped copies land before config.json, the file that makes the key real.
  writeAtomic(paths.recovery, JSON.stringify(wrapWithCode(masterKey, recoveryCode)));
  storeWithOs(stateDir, safeStorage, masterKey);
  writeAtomic(paths.config, JSON.stringify({
    v: 1, enabled: true, key_id: keyId(masterKey), created_at: Math.floor(now() / 1000),
  }));
  return { masterKey, recoveryCode };
}

/** New sessions stay plain. The key stays, so sealed sessions stay readable. */
function disable({ stateDir }) {
  const config = readConfig(stateDir);
  if (!config) return;
  writeAtomic(sealPaths(stateDir).config, JSON.stringify({ ...config, enabled: false }));
}

/** Remove a key that no backend received. Only for a failed first enable. */
function forget({ stateDir }) {
  const paths = sealPaths(stateDir);
  for (const file of [paths.config, paths.os, paths.recovery]) fs.rmSync(file, { force: true });
}

/** Unwrap the key with the recovery code and store it in this system's key store. */
function recover({ stateDir, safeStorage, platform = process.platform, code }) {
  const config = readConfig(stateDir);
  if (!config) throw sealError("SEAL_NOT_CONFIGURED");
  const masterKey = unwrapWithCode(readJson(sealPaths(stateDir).recovery), code);
  if (masterKey.length !== KEY_BYTES || keyId(masterKey) !== config.key_id) {
    throw sealError("SEAL_RECOVERY_REJECTED");
  }
  if (protectionOf(safeStorage, platform) === "os") storeWithOs(stateDir, safeStorage, masterKey);
  return masterKey;
}

/** The one stdin line for HARNESS_SEAL_STDIN, or null when encryption is not set up. */
function spawnLine({ stateDir, safeStorage, platform = process.platform }) {
  if (!readConfig(stateDir)) return null;
  const masterKey = loadMasterKey({ stateDir, safeStorage, platform });
  return JSON.stringify(masterKey ? { master_key: masterKey.toString("base64") } : {}) + "\n";
}

module.exports = {
  sealPaths,
  keyId,
  readConfig,
  newRecoveryCode,
  loadMasterKey,
  status,
  enable,
  disable,
  forget,
  recover,
  spawnLine,
};
