import type { Item, Msg } from "../TranscriptList";

export function getSimilarity(s1: string, s2: string): number {
  const norm1 = s1.toLowerCase().replace(/[^a-z0-9]/g, "");
  const norm2 = s2.toLowerCase().replace(/[^a-z0-9]/g, "");

  if (!norm1 || !norm2) return 0;
  if (norm1 === norm2) return 1.0;

  if (norm1.startsWith(norm2) || norm2.startsWith(norm1)) {
    return 1.0;
  }

  const w1 = s1.toLowerCase().replace(/[^a-z0-9\s]/g, "").split(/\s+/).filter(Boolean);
  const w2 = s2.toLowerCase().replace(/[^a-z0-9\s]/g, "").split(/\s+/).filter(Boolean);
  const set1 = new Set(w1);
  const set2 = new Set(w2);
  let intersect = 0;
  set1.forEach(w => {
    if (set2.has(w)) intersect++;
  });
  const wordJaccard = intersect / (set1.size + set2.size - intersect);

  const getBigrams = (s: string) => {
    const bigrams = new Set<string>();
    for (let i = 0; i < s.length - 1; i++) {
      bigrams.add(s.substring(i, i + 2));
    }
    return bigrams;
  };
  const b1 = getBigrams(norm1);
  const b2 = getBigrams(norm2);
  if (b1.size > 0 && b2.size > 0) {
    let bIntersect = 0;
    b1.forEach(b => {
      if (b2.has(b)) bIntersect++;
    });
    const charJaccard = bIntersect / (b1.size + b2.size - bIntersect);
    return Math.max(wordJaccard, charJaccard);
  }

  return wordJaccard;
}

/**
 * Drop near-duplicate assistant narration within a turn.
 *
 * Pilots often restate the same diagnosis after each tool ("Found the root
 * causes…") with cards between the bubbles -- consecutive-only dedupe missed
 * that and left the user reading the same paragraph twice while tokens burned.
 * Scan back past cards/thinking within the current user turn; keep the longer
 * copy when similarity is high.
 */
export function deduplicateAssistantNarration(items: Item[]): Item[] {
  const result: Item[] = [];
  // Indices into `result` of assistant msgs since the last user msg.
  let turnAssistantIdx: number[] = [];

  for (const item of items) {
    if (item.kind === "msg" && item.msg.role === "user") {
      turnAssistantIdx = [];
      result.push(item);
      continue;
    }

    if (item.kind === "msg" && item.msg.role === "assistant") {
      // Never collapse an open stream into a prior bubble -- the typewriter
      // still owns it; finalize path will re-run this after streaming:false.
      if (item.msg.streaming) {
        result.push(item);
        turnAssistantIdx.push(result.length - 1);
        continue;
      }

      const newText = item.msg.text || "";
      let dupIdx = -1;
      for (let i = turnAssistantIdx.length - 1; i >= 0; i--) {
        const prev = result[turnAssistantIdx[i]];
        if (!prev || prev.kind !== "msg") continue;
        if (prev.msg.streaming) continue;
        if (getSimilarity(prev.msg.text || "", newText) > 0.85) {
          dupIdx = turnAssistantIdx[i];
          break;
        }
      }

      if (dupIdx >= 0) {
        const prev = result[dupIdx] as { kind: "msg"; msg: Msg };
        if (newText.length > (prev.msg.text || "").length) {
          result[dupIdx] = item;
        }
        continue;
      }

      result.push(item);
      turnAssistantIdx.push(result.length - 1);
      continue;
    }

    result.push(item);
  }
  return result;
}

/** @deprecated use deduplicateAssistantNarration -- kept as alias for callers. */
export function deduplicateConsecutiveAssistantMessages(items: Item[]): Item[] {
  return deduplicateAssistantNarration(items);
}

/**
 * Drop consecutive duplicate tool cards / swarm badges (same id).
 * Session-switch SSE races can re-append events already present after a
 * transcript poll replace; without this the Investigated block repeats forever.
 */
export function dedupeDisplayItems(items: Item[]): Item[] {
  const out: Item[] = [];
  const seenCardIds = new Set<string>();
  const seenSwarmIds = new Set<string>();
  for (const item of items) {
    if (item.kind === "card" && item.card?.id) {
      const id = String(item.card.id);
      if (seenCardIds.has(id)) continue;
      seenCardIds.add(id);
    } else if (item.kind === "swarm_result" && item.job_id) {
      const id = String(item.job_id);
      if (seenSwarmIds.has(id)) continue;
      seenSwarmIds.add(id);
    }
    out.push(item);
  }
  return out;
}

/** Map /api/sessions/transcript payload into transcript Item rows. */
export function transcriptResponseToItems(res: {
  history?: any[];
  display?: any[];
}): Item[] {
  let loadedItems: Item[] = [];
  if (res.display && res.display.length > 0) {
    loadedItems = res.display.map((m: any) => {
      if (m.type === "card") {
        // result == null means still in flight (persisted at action_start).
        const pending = m.result == null;
        return {
          kind: "card" as const,
          card: {
            id: m.id,
            goal: m.goal,
            cwd: m.cwd || null,
            kind: m.kind,
            running: pending,
            open: false,
            result: pending ? undefined : (m.result || undefined)
          }
        };
      } else if (m.type === "swarm_result") {
        return {
          kind: "swarm_result" as const,
          job_id: m.job_id || "",
          applied: !!m.applied,
          files: Array.isArray(m.files) ? m.files : [],
          summary: m.summary || "",
          error: m.error || null,
          objective: m.objective || ""
        };
      } else {
        return {
          kind: "msg" as const,
          msg: {
            role: m.role as "user" | "assistant",
            text: m.text || ""
          }
        };
      }
    });
  } else {
    loadedItems = (res.history || [])
      .filter((m: any) => m.role === "assistant" || (m.role === "user" && m.content && !m.content.startsWith("(")))
      .map((m: any) => ({
        kind: "msg" as const,
        msg: {
          role: m.role as "user" | "assistant",
          text: m.content || ""
        }
      }));
  }
  return deduplicateConsecutiveAssistantMessages(dedupeDisplayItems(loadedItems));
}

function cardCount(items: Item[]): number {
  let n = 0;
  for (const it of items) if (it.kind === "card") n += 1;
  return n;
}

function runningCardIds(items: Item[]): Set<string> {
  const ids = new Set<string>();
  for (const it of items) {
    if (it.kind === "card" && it.card.running && it.card.id) {
      ids.add(String(it.card.id));
    }
  }
  return ids;
}

/**
 * True when applying `remote` (sessionTranscript poll) would erase live tool
 * rows the SSE stream already painted -- the Investigating blink / disappear
 * bug while run_command is still going.
 */
export function shouldPreferLocalTranscript(local: Item[], remote: Item[]): boolean {
  const localRunning = runningCardIds(local);
  if (localRunning.size > 0) {
    for (const id of localRunning) {
      const rem = remote.find((it) => it.kind === "card" && it.card.id === id);
      if (!rem) return true;
      // Remote still pending (result null → running) or completed: ok to take remote.
    }
  }
  // Never shrink the tool timeline mid-session; poll payloads can lag saves.
  if (cardCount(local) > cardCount(remote)) return true;
  return false;
}

/**
 * Merge a disk/API transcript into the live feed without dropping in-flight
 * cards. Prefer remote message text when ids match; keep local-only cards.
 */
export function mergeTranscriptItems(local: Item[], remote: Item[]): Item[] {
  if (!shouldPreferLocalTranscript(local, remote)) return remote;
  const remoteByCardId = new Map<string, Extract<Item, { kind: "card" }>>();
  for (const it of remote) {
    if (it.kind === "card" && it.card.id) {
      remoteByCardId.set(String(it.card.id), it);
    }
  }
  const merged = local.map((it) => {
    if (it.kind !== "card" || !it.card.id) return it;
    const rem = remoteByCardId.get(String(it.card.id));
    if (!rem) return it;
    // Remote finished the tool -- take its result, drop running.
    if (!rem.card.running && rem.card.result) {
      return {
        kind: "card" as const,
        card: {
          ...it.card,
          running: false,
          result: rem.card.result,
          goal: rem.card.goal || it.card.goal,
          kind: rem.card.kind || it.card.kind,
        },
      };
    }
    return it;
  });
  // Append remote cards the local feed never saw (reattach gap).
  const localIds = new Set(
    local.filter((it) => it.kind === "card" && it.card.id).map((it) => String((it as Extract<Item, { kind: "card" }>).card.id)),
  );
  for (const it of remote) {
    if (it.kind === "card" && it.card.id && !localIds.has(String(it.card.id))) {
      merged.push(it);
    }
  }
  return merged;
}

/** Cheap content fingerprint so busy-poll refresh can skip identical payloads. */
export function transcriptFingerprint(items: Item[]): string {
  let fp = `n=${items.length}`;
  for (let i = 0; i < items.length; i++) {
    const it = items[i];
    if (it.kind === "msg") {
      fp += `|m:${it.msg.role}:${it.msg.text.length}:${it.msg.streaming ? 1 : 0}`;
    } else if (it.kind === "card") {
      const r = it.card.result;
      fp += `|c:${it.card.id}:${it.card.running ? 1 : 0}:${r ? 1 : 0}`;
    } else if (it.kind === "swarm_result") {
      fp += `|s:${it.job_id}:${it.applied ? 1 : 0}`;
    } else if (it.kind === "thinking") {
      fp += `|t:${(it.text || "").length}:${(it as { streaming?: boolean }).streaming ? 1 : 0}`;
    } else if (it.kind === "tool_prep") {
      fp += `|p:${String((it as { name?: string }).name || "")}`;
    } else {
      fp += `|o:${it.kind}`;
    }
  }
  return fp;
}
