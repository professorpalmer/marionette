import type { Settings } from "../lib/api";

const SETTINGS_SNAPSHOT_KEY = "pmharness.settings.snapshot";
let memorySettingsSnapshot: Settings | null = null;

/** Persist only safe display/config fields — never raw keys or tokens. */
export function toSafeSettingsSnapshot(s: Settings): Settings {
  return {
    driver: s.driver,
    reach: s.reach,
    budget: s.budget,
    models: s.models,
    auto_distill: s.auto_distill,
    reviewEditsBeforeApply: s.reviewEditsBeforeApply,
    autoVerify: s.autoVerify,
    autoCommandGuard: s.autoCommandGuard,
    hash_edit_enabled: s.hash_edit_enabled,
    commandTimeout: s.commandTimeout,
    maxPilotSteps: s.maxPilotSteps,
    pilotToolBudget: s.pilotToolBudget,
    workerTokenBudget: s.workerTokenBudget,
    reasoning_effort: s.reasoning_effort,
    compactionResidual: s.compactionResidual,
    wiki_auto: s.wiki_auto,
    state_dir: s.state_dir,
    repo: s.repo,
    has_api_key: s.has_api_key,
    api_key_masked: s.api_key_masked,
    key_env_var: s.key_env_var,
    preflight_ok: s.preflight_ok,
    bedrock: s.bedrock
      ? {
          configured: s.bedrock.configured,
          has_key: s.bedrock.has_key,
          auth_mode: s.bedrock.auth_mode,
          masked: s.bedrock.masked,
          has_bearer: s.bedrock.has_bearer,
          has_access_key: s.bedrock.has_access_key,
          has_session_token: s.bedrock.has_session_token,
          region: s.bedrock.region,
          aws_region: s.bedrock.aws_region,
          bedrock_region: s.bedrock.bedrock_region,
          model_id: s.bedrock.model_id,
          disconnected: s.bedrock.disconnected,
        }
      : undefined,
  };
}

export function clearSettingsSnapshot() {
  memorySettingsSnapshot = null;
  try {
    localStorage.removeItem(SETTINGS_SNAPSHOT_KEY);
  } catch {
    /* ignore */
  }
}

export function readSettingsSnapshot(): Settings | null {
  if (memorySettingsSnapshot) return memorySettingsSnapshot;
  try {
    const raw = localStorage.getItem(SETTINGS_SNAPSHOT_KEY);
    const parsed = raw ? JSON.parse(raw) : null;
    if (parsed?.settings && typeof parsed.settings === "object") {
      memorySettingsSnapshot = parsed.settings as Settings;
      return memorySettingsSnapshot;
    }
  } catch {
    /* stale/corrupt cache is disposable */
  }
  return null;
}

export function writeSettingsSnapshot(settings: Settings) {
  const safe = toSafeSettingsSnapshot(settings);
  memorySettingsSnapshot = safe;
  try {
    localStorage.setItem(
      SETTINGS_SNAPSHOT_KEY,
      JSON.stringify({ settings: safe, savedAt: Date.now() }),
    );
  } catch {
    /* storage pressure must not break Settings */
  }
}
