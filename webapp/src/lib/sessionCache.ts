import { getJSONSoft, postJSON } from "./transport";

export type CacheState = "off" | "cold" | "warming" | "warm" | "error" | "unsupported";
export type SessionCache = {
  session_id: string;
  state: CacheState;
  enabled: boolean;
  retain_reasoning: boolean;
  reason: string;
  refreshes: number;
  max_refreshes: number;
  idle_seconds: number;
  max_spend_usd: number;
  reserved_usd: number;
};
export type CacheCommand = { action: "start" | "stop" } | { action: "preferences"; retain_reasoning: boolean };

function parseSessionCache(value: unknown, sessionId: string): SessionCache {
  if (!value || typeof value !== "object" || !("session_id" in value) || value.session_id !== sessionId
    || !("state" in value) || !(value.state === "off" || value.state === "cold" || value.state === "warming"
      || value.state === "warm" || value.state === "error" || value.state === "unsupported")
    || !("enabled" in value) || typeof value.enabled !== "boolean"
    || !("retain_reasoning" in value) || typeof value.retain_reasoning !== "boolean"
    || !("reason" in value) || typeof value.reason !== "string"
    || !("refreshes" in value) || typeof value.refreshes !== "number"
    || !("max_refreshes" in value) || typeof value.max_refreshes !== "number"
    || !("idle_seconds" in value) || typeof value.idle_seconds !== "number"
    || !("max_spend_usd" in value) || typeof value.max_spend_usd !== "number"
    || !("reserved_usd" in value) || typeof value.reserved_usd !== "number") {
    throw new Error("Cache controls are unavailable for this session.");
  }
  return { session_id: sessionId, state: value.state, enabled: value.enabled,
    retain_reasoning: value.retain_reasoning, reason: value.reason, refreshes: value.refreshes,
    max_refreshes: value.max_refreshes, idle_seconds: value.idle_seconds,
    max_spend_usd: value.max_spend_usd, reserved_usd: value.reserved_usd };
}

export async function getSessionCache(sessionId: string): Promise<SessionCache> {
  if (!sessionId) throw new Error("Select a session first.");
  return parseSessionCache(await getJSONSoft<unknown>(`/api/session/cache?session_id=${encodeURIComponent(sessionId)}`), sessionId);
}

export async function updateSessionCache(sessionId: string, command: CacheCommand): Promise<SessionCache> {
  if (!sessionId) throw new Error("Select a session first.");
  const result = parseSessionCache(await postJSON<unknown>("/api/session/cache", { session_id: sessionId, ...command }), sessionId);
  window.dispatchEvent(new CustomEvent("harness-cache-updated", { detail: sessionId }));
  return result;
}
