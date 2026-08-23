/**
 * Electron main-process connector vault.
 * Values are encrypted with safeStorage when available and never returned
 * to the renderer except as present|missing.
 */
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const ENV_BINDINGS = {
  "pypi::token": [
    ["TWINE_USERNAME", "__token__"],
    ["TWINE_PASSWORD", null],
    ["PYPI_TOKEN", null],
  ],
  "portable-llm-wiki::WIKI_OWNER_TOKEN": [["WIKI_OWNER_TOKEN", null]],
  "slack::token": [
    ["SLACK_BOT_TOKEN", null],
    ["SLACK_TOKEN", null],
  ],
};

function resolveStateDir(env) {
  const explicit = String((env || process.env).HARNESS_STATE_DIR || "").trim();
  if (explicit) return path.resolve(explicit);
  return path.join(os.homedir(), ".pmharness");
}

function vaultPath(stateDir) {
  return path.join(stateDir, "secret-vault.enc.json");
}

function emptyStore() {
  return { v: 1, agents: {} };
}

function readStore(file) {
  try {
    const raw = fs.readFileSync(file, "utf8");
    const data = JSON.parse(raw);
    if (data && typeof data === "object" && data.agents && typeof data.agents === "object") {
      return data;
    }
  } catch (_) {
    /* missing or corrupt: start empty */
  }
  return emptyStore();
}

function writeStore(file, store) {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  const tmp = `${file}.${process.pid}.tmp`;
  fs.writeFileSync(tmp, JSON.stringify(store, null, 2), { encoding: "utf8", mode: 0o600 });
  fs.renameSync(tmp, file);
  try {
    fs.chmodSync(file, 0o600);
  } catch (_) {
    /* best-effort */
  }
}

function encryptValue(safeStorage, plain) {
  if (safeStorage && typeof safeStorage.isEncryptionAvailable === "function" && safeStorage.isEncryptionAvailable()) {
    const buf = safeStorage.encryptString(plain);
    return { alg: "safeStorage", data: buf.toString("base64") };
  }
  return { alg: "b64", data: Buffer.from(plain, "utf8").toString("base64") };
}

function decryptValue(safeStorage, blob) {
  if (!blob || typeof blob !== "object") return "";
  try {
    if (blob.alg === "safeStorage" && safeStorage && typeof safeStorage.decryptString === "function") {
      return safeStorage.decryptString(Buffer.from(blob.data, "base64"));
    }
    if (blob.alg === "b64") {
      return Buffer.from(blob.data, "base64").toString("utf8");
    }
  } catch (_) {
    return "";
  }
  return "";
}

function normalizeAgentId(value) {
  return String(value || "").trim() || "default";
}

function normalizeConnector(value) {
  return String(value || "").trim().toLowerCase();
}

function normalizeField(value) {
  return String(value || "").trim();
}

function putSecret(opts) {
  const stateDir = opts.stateDir || resolveStateDir(opts.env);
  const file = vaultPath(stateDir);
  const agentId = normalizeAgentId(opts.agentId);
  const connector = normalizeConnector(opts.connector);
  const field = normalizeField(opts.field);
  const value = String(opts.value || "").trim();
  if (!connector || !field || !value) {
    return { ok: false, error: "connector, field, and value are required" };
  }
  const store = readStore(file);
  store.agents[agentId] = store.agents[agentId] || {};
  store.agents[agentId][connector] = store.agents[agentId][connector] || {};
  store.agents[agentId][connector][field] = encryptValue(opts.safeStorage, value);
  writeStore(file, store);
  return { ok: true, present: true, connector, field, state: "present" };
}

function presenceOf(opts) {
  const stateDir = opts.stateDir || resolveStateDir(opts.env);
  const file = vaultPath(stateDir);
  const agentId = normalizeAgentId(opts.agentId);
  const connector = normalizeConnector(opts.connector);
  const field = normalizeField(opts.field);
  const store = readStore(file);
  const byAgent = store.agents[agentId] || {};
  if (connector && field) {
    const present = Boolean((byAgent[connector] || {})[field]);
    return {
      agent_id: agentId,
      connector,
      field,
      present,
      state: present ? "present" : "missing",
    };
  }
  const connectors = {};
  for (const [conn, fields] of Object.entries(byAgent)) {
    if (!fields || typeof fields !== "object") continue;
    const row = {};
    for (const name of Object.keys(fields)) row[name] = "present";
    if (Object.keys(row).length) connectors[conn] = row;
  }
  return { agent_id: agentId, connectors };
}

function envBindingsFor(connector, field, value) {
  const key = `${normalizeConnector(connector)}::${normalizeField(field)}`;
  const out = {};
  for (const [name, lit] of ENV_BINDINGS[key] || []) {
    out[name] = lit == null ? value : lit;
  }
  const generic = `${normalizeConnector(connector).toUpperCase().replace(/-/g, "_")}_${normalizeField(field).toUpperCase()}`;
  if (!out[generic]) out[generic] = value;
  return out;
}

function injectEnv(opts) {
  const stateDir = opts.stateDir || resolveStateDir(opts.env);
  const file = vaultPath(stateDir);
  const agentId = normalizeAgentId(opts.agentId);
  const store = readStore(file);
  const byAgent = store.agents[agentId] || {};
  const target = opts.target || {};
  for (const [conn, fields] of Object.entries(byAgent)) {
    if (!fields || typeof fields !== "object") continue;
    for (const [name, blob] of Object.entries(fields)) {
      const value = decryptValue(opts.safeStorage, blob);
      if (!value) continue;
      Object.assign(target, envBindingsFor(conn, name, value));
    }
  }
  return target;
}

function redactText(text) {
  if (!text) return text;
  return String(text).replace(
    /\b(TWINE_PASSWORD|PYPI_TOKEN|WIKI_OWNER_TOKEN|SLACK_BOT_TOKEN|SLACK_TOKEN)\s*[=:]\s*\S+/gi,
    (m) => m.split(/[=:]/)[0] + "=REDACTED",
  );
}

module.exports = {
  resolveStateDir,
  vaultPath,
  putSecret,
  presenceOf,
  injectEnv,
  envBindingsFor,
  redactText,
  encryptValue,
  decryptValue,
};
