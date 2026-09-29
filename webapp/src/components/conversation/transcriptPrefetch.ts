/**
 * Warm the session-switch transcript cache ahead of a click.
 * Cold→cold swaps blanked the feed for one paint; hover/idle prefetch
 * makes the next select a cache hit (same path as flip-flop warming).
 */
import { api } from "../../lib/api";
import {
  isShownTranscriptSession,
  peekTranscriptCacheEntry,
  writeTranscriptCache,
} from "./transcriptCache";
import { transcriptResponseToItems } from "./transcriptItems";

const inFlight = new Map<string, Promise<boolean>>();

/** Prefetch one session transcript into the warm cache. No-ops on hit. */
export function prefetchSessionTranscript(sessionId: string): Promise<boolean> {
  const id = (sessionId || "").trim();
  if (!id) return Promise.resolve(false);
  if (peekTranscriptCacheEntry(id) || isShownTranscriptSession(id)) return Promise.resolve(false);
  const existing = inFlight.get(id);
  if (existing) return existing;
  const work = (async () => {
    try {
      const res = await api.sessionTranscript(id);
      // Another path may have filled the cache, or opened this session, while
      // we were in flight; the shown session's own hydrate owns its entry.
      if (peekTranscriptCacheEntry(id) || isShownTranscriptSession(id)) return false;
      writeTranscriptCache(id, transcriptResponseToItems(res, id));
      return true;
    } catch {
      return false;
    } finally {
      inFlight.delete(id);
    }
  })();
  inFlight.set(id, work);
  return work;
}

/** Bounded idle prefetch for visible rail rows (open sessions first). */
export async function prefetchSessionTranscripts(
  sessionIds: readonly string[],
  limit = 6,
): Promise<void> {
  const unique: string[] = [];
  const seen = new Set<string>();
  for (const raw of sessionIds) {
    const id = (raw || "").trim();
    if (!id || seen.has(id) || peekTranscriptCacheEntry(id)) continue;
    seen.add(id);
    unique.push(id);
    if (unique.length >= limit) break;
  }
  const concurrency = 2;
  let i = 0;
  const workers = Array.from({ length: Math.min(concurrency, unique.length) }, async () => {
    while (i < unique.length) {
      const next = unique[i++];
      await prefetchSessionTranscript(next);
    }
  });
  await Promise.all(workers);
}
