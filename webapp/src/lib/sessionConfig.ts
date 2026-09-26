import type { Config } from "./api";

/**
 * Keep composer models and the workspace repo when /api/config is still
 * fenced to another session_id. Nulling the whole payload empties the
 * picker and JobMetadataOwner (empty repo invalidates the dashboard).
 */
export function configForActiveSession(
  received: Config | null,
  activeSessionId: string | null,
): Config | null {
  if (!received) return null;
  if (!activeSessionId) return received;
  if (!received.session_id || received.session_id === activeSessionId) return received;
  return { ...received, session_id: activeSessionId };
}
