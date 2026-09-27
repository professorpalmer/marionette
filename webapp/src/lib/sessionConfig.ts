import type { Config } from "./api";

/**
 * Keep composer models and the workspace repo when /api/config is still
 * fenced to another session_id. Nulling the whole payload empties the
 * picker and JobMetadataOwner (empty repo invalidates the dashboard).
 *
 * Session-owned fields (harness/server.py _get_config overlays them from
 * pilot_preferences) are cleared, not relabeled: the previous session's
 * driver must never show as, or be sent as, this session's model.
 */
export type SessionPilotFields = Pick<Config, "driver" | "reasoning_effort" | "swarm_reasoning_effort">;

/** This session's own pilot fields from a config it answered, if any. */
export function sessionPilotFields(config: Config | null): SessionPilotFields | null {
  if (!config?.session_id || !config.driver) return null;
  return {
    driver: config.driver,
    reasoning_effort: config.reasoning_effort,
    swarm_reasoning_effort: config.swarm_reasoning_effort,
  };
}

/**
 * ``known`` is this session's last answered pilot fields. Returning to a
 * session shows them at once instead of blanking the pickers until its
 * config lands; the fresh payload replaces them when it arrives.
 */
export function configForActiveSession(
  received: Config | null,
  activeSessionId: string | null,
  known?: SessionPilotFields | null,
): Config | null {
  if (!received) return null;
  if (!activeSessionId) return received;
  if (!received.session_id || received.session_id === activeSessionId) return received;
  const workspace: Config = { ...received, session_id: null, driver: "" };
  delete workspace.reasoning_effort;
  delete workspace.swarm_reasoning_effort;
  if (!known?.driver) return workspace;
  return { ...workspace, ...known, session_id: activeSessionId };
}

/**
 * What the composer pickers render: this session's config, or a
 * workspace-only config (empty driver, shown as loading) while the
 * session's own config is pending. Another session's config is refused.
 */
export function pickerConfig(config: Config | null, sessionId?: string | null): Config | null {
  if (!config) return null;
  if (!sessionId || config.session_id === sessionId || !config.driver) return config;
  return null;
}
