"use strict";

const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

function isInspectMode(env = process.env) {
  const raw = String((env && env.HARNESS_INSPECT) || "").trim().toLowerCase();
  return raw === "1" || raw === "true" || raw === "yes";
}

function pmharnessHome() {
  return path.join(os.homedir(), ".pmharness");
}

function resolveHarnessStateDir(env = process.env) {
  const explicit = String((env && env.HARNESS_STATE_DIR) || "").trim();
  if (explicit) {
    return path.resolve(explicit);
  }
  const stateSub = path.join(pmharnessHome(), "state");
  try {
    if (fs.statSync(stateSub).isDirectory()) {
      return stateSub;
    }
  } catch {}
  return pmharnessHome();
}

function resolveInspectUserDataDir(env = process.env) {
  const explicit = String((env && env.HARNESS_USER_DATA_DIR) || "").trim();
  if (explicit) {
    return path.resolve(explicit);
  }
  return "";
}

function buildPuppetmasterBackendEnv(env, options = {}) {
  const out = {
    ...env,
    PUPPETMASTER_MODELS_PATH: options.modelsPath,
  };
  delete out.PUPPETMASTER_ONLY_ADAPTERS;
  delete out.PUPPETMASTER_DISABLED_PROVIDERS;
  if (options.stateDir) {
    out.PUPPETMASTER_STATE_DIR = options.stateDir;
  } else {
    delete out.PUPPETMASTER_STATE_DIR;
  }
  return out;
}

function stateFileSearchDirs(env = process.env) {
  const explicit = resolveHarnessStateDir(env);
  if (isInspectMode(env)) {
    return [explicit];
  }
  const home = pmharnessHome();
  if (path.resolve(explicit) === path.resolve(home)) {
    return [explicit];
  }
  return [explicit, home];
}

module.exports = {
  buildPuppetmasterBackendEnv,
  isInspectMode,
  pmharnessHome,
  resolveHarnessStateDir,
  resolveInspectUserDataDir,
  stateFileSearchDirs,
};
