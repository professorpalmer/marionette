import { transcriptFingerprint } from "./transcriptItems";
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
 * Prefer prefetch (rail hover / idle) so misses are rare. An uncached switch
 * still blanks rather than painting priorItems (cross-session relic risk);
 * the feed stays full opacity and silent while loading (no dim / Loading copy).
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
export type CachedTranscript = {
  items: Item[];
  /**
   * True only for New Session's pre-switch `[]` seed. Distinguishes intentional
   * blank from an empty/evicted cache that should still retry disk hydrate.
   */
  seededEmpty?: boolean;
};

const transcriptCacheBySessionId = new Map<string, CachedTranscript>();

/** Test helper: drop all warm-cache entries. */
export function clearTranscriptCache() {
  transcriptCacheBySessionId.clear();
}

/** Test helper: read cached items for a session (undefined on miss). */
export function peekTranscriptCache(sessionId: string): Item[] | undefined {
  return transcriptCacheBySessionId.get(sessionId)?.items;
}

/** Full cache entry (items + seededEmpty), undefined on miss. */
export function peekTranscriptCacheEntry(
  sessionId: string,
): CachedTranscript | undefined {
  const entry = transcriptCacheBySessionId.get(sessionId);
  if (!entry) return undefined;
  return {
    items: entry.items,
    seededEmpty: entry.seededEmpty === true,
  };
}

export type WriteTranscriptCacheOpts = {
  /** Mark New Session seed — skip empty-transcript retry / fail banner. */
  seededEmpty?: boolean;
  /**
   * Keep the same array identity so a retained TranscriptList can resume
   * without remounting markdown (session-pane keep-alive).
   */
  retainRef?: boolean;
};

/** Seed or overwrite the warm cache for a session. */
export function writeTranscriptCache(
  sessionId: string,
  items: Item[],
  opts?: WriteTranscriptCacheOpts,
) {
  const seededEmpty = opts?.seededEmpty === true && items.length === 0;
  transcriptCacheBySessionId.set(sessionId, {
    items: opts?.retainRef ? items : [...items],
    ...(seededEmpty ? { seededEmpty: true } : {}),
  });
}

/** A disk response may replace rows only while its local baseline still owns them. */
export function captureTranscriptRead(
  sessionId: string,
  itemsRef: { current: Item[] },
  streamGenRef: { current: number },
): () => boolean {
  const items = itemsRef.current;
  const fingerprint = transcriptFingerprint(items);
  const streamGen = streamGenRef.current;
  const cached = peekTranscriptCache(sessionId);
  return () => itemsRef.current === items
    && transcriptFingerprint(itemsRef.current) === fingerprint
    && streamGenRef.current === streamGen
    && peekTranscriptCache(sessionId) === cached;
}
