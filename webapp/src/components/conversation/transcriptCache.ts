import type { Item } from "../TranscriptList";

/**
 * Session-switch transcript hydrate: decide what to show while the target
 * transcript loads.
 *
 * - cache hit -> show cached items (authoritative for that session)
 * - cache miss -> empty + stale (loading). Never paint priorItems: that leaked
 *   session A's Investigated/swarm chunks into a brand-new empty session B.
 * - cleared session id -> empty is correct
 *
 * A brief empty flash on an uncached switch is preferable to cross-session
 * relic paint. Warm-cache hits still hydrate instantly with no flash.
 */
export function resolveSwitchTranscript(args: {
  nextId: string | null;
  cached: Item[] | undefined;
  priorItems: Item[];
}): { items: Item[]; stale: boolean; blank: boolean } {
  if (!args.nextId) {
    return { items: [], stale: false, blank: true };
  }
  if (args.cached) {
    return { items: args.cached, stale: false, blank: false };
  }
  // priorItems intentionally unused: never show another session's rows.
  void args.priorItems;
  return { items: [], stale: true, blank: false };
}

// Per-session transcript warm cache (Hermes-style sessionStateByRuntimeIdRef).
// Survives activeSessionId switches so the UI hydrates instantly and a background
// sessionTranscript refresh can land without blanking a cache hit. Module-level
// so the map outlives a single Conversation mount within the SPA lifetime.
type CachedTranscript = { items: Item[] };
const transcriptCacheBySessionId = new Map<string, CachedTranscript>();

/** Test helper: drop all warm-cache entries. */
export function clearTranscriptCache() {
  transcriptCacheBySessionId.clear();
}

/** Test helper: read cached items for a session (undefined on miss). */
export function peekTranscriptCache(sessionId: string): Item[] | undefined {
  return transcriptCacheBySessionId.get(sessionId)?.items;
}

/** Seed or overwrite the warm cache for a session. */
export function writeTranscriptCache(sessionId: string, items: Item[]) {
  transcriptCacheBySessionId.set(sessionId, { items: [...items] });
}
