import type { Item, Msg } from "../TranscriptList";
import { isTrivialAssistantCrumb, sanitizeThinkingStatusGlue } from "./thinkingToolPrep";

/**
 * Short shared prefixes ("I will") must never suppress a distinct post-tool
 * answer. Cursor-gap replay chunks are typically longer fragments of the
 * sealed bubble; require this many trimmed chars before prefix/suffix cover.
 */
export const PROSE_COVER_MIN_CHUNK = 12;

/**
 * True when `existing` already holds `incoming` as exact text or a proven
 * continuation fragment (substantial prefix/suffix of the sealed bubble).
 * Bare mid-string `includes` and short shared prefixes are not cover.
 */
export function assistantProseCovers(existing: string, incoming: string): boolean {
  const a = (existing || "").trim();
  const b = (incoming || "").trim();
  if (!a || !b) return false;
  if (a === b) return true;
  if (b.length < PROSE_COVER_MIN_CHUNK) return false;
  // Proven replay only: chunk is already painted as a prefix/suffix of sealed.
  // Do not treat incoming-longer (b.startsWith(a)) as cover — that is a new answer.
  if (a.startsWith(b) || a.endsWith(b)) return true;
  return false;
}

/**
 * Open-bubble absorb. Pilot prose accumulates VERBATIM: the final `message`
 * for this step must equal the streamed bubble byte-for-byte, or the sealed
 * bubble and the final paint as two answers. No glue sanitizing, no crumb
 * drops (a lone `**` delta is half of a bold marker), no prefix/suffix
 * heuristics (a later `###` chunk is not a replay of the opening `###`).
 *
 * The only thing absorbed is a Completions cumulative snapshot: the provider
 * resends everything so far (or everything twice). Both start with the whole
 * accumulated text, which no real delta can do once `acc` has any length.
 */
export function absorbOpenPilotDelta(existing: string, incoming: string): string {
  const acc = existing || "";
  const inc = incoming || "";
  if (!inc) return acc;
  if (!acc) return inc;
  if (inc.startsWith(acc)) {
    const rest = inc.slice(acc.length);
    if (!rest.trim()) {
      return acc.length >= PROSE_COVER_MIN_CHUNK ? acc : acc + inc;
    }
    if (acc.length >= PROSE_COVER_MIN_CHUNK && rest.trim() === acc.trim()) return acc;
    return inc;
  }
  if (acc.length >= PROSE_COVER_MIN_CHUNK && inc.trim() === acc.trim()) return acc;
  return acc + inc;
}

/** Current-turn sealed (non-streaming) assistant texts, newest last. */
export function sealedAssistantTextsInTurn(items: Item[]): string[] {
  const texts: string[] = [];
  for (let i = items.length - 1; i >= 0; i--) {
    const it = items[i];
    if (it.kind === "msg" && it.msg.role === "user") break;
    if (it.kind !== "msg" || it.msg.role !== "assistant") continue;
    if (it.msg.streaming || it.msg.workerStream) continue;
    const t = (it.msg.text || "").trim();
    if (t) texts.push(t);
  }
  return texts.reverse();
}

/**
 * Durable hydrate + ring replay guard: when there is no open pilot stream,
 * skip deltas whose prose is already present in a sealed assistant bubble.
 */
export function sealedAssistantCoversDelta(items: Item[], chunk: string): boolean {
  const piece = (chunk || "").trim();
  if (!piece) return false;
  if (findStreamingBubbleIdx(items, { excludeWorkerStream: true }) >= 0) {
    return false;
  }
  for (const text of sealedAssistantTextsInTurn(items)) {
    if (assistantProseCovers(text, piece)) return true;
  }
  return false;
}

/**
 * Find the open streaming assistant bubble.
 *
 * When `streamId` is set, search the current turn for that identity even if
 * thinking rows or other channels sit after it — ownership is by stream_id,
 * not arrival order. Tool/prep cards are still a hard phase fence: a same-
 * stream delta after a card opens a NEW bubble at the end (new segment) so
 * the transcript stays append-only and never grows text above a card.
 *
 * Without stream identity: skip ephemeral decoration that may land while the
 * typewriter drains (thinking rows, codegraph chips). Do NOT scan past
 * tool/prep cards: once a card exists after an assistant bubble, later deltas
 * must open a post-card bubble rather than resume pre-tool narration.
 *
 * When excludeWorkerStream is set, skip ephemeral swarm worker preview bubbles
 * so the pilot's open bubble is finalized instead.
 * When workerStreamOnly is set, only match assistant streaming msgs tagged
 * workerStream (never the pilot bubble). Optional workerId further keys the
 * match so parallel implement/swarm workers each keep their own preview.
 */
export function findStreamingBubbleIdx(
  items: Item[],
  opts?: {
    excludeWorkerStream?: boolean;
    workerStreamOnly?: boolean;
    workerId?: string;
    streamId?: string;
  },
): number {
  const streamId = (opts?.streamId || "").trim();
  const wantWorkerId = (opts?.workerId || "").trim();
  const matchesStreamAffinity = (m: Msg): boolean => {
    if (opts?.workerStreamOnly) {
      if (!m.workerStream) return false;
      if (!wantWorkerId) return true;
      const have = (m.worker_id || "").trim();
      // Legacy untagged preview can absorb the first tagged worker; once stamped
      // (ensureWorkerStreamingBubble / append), peers must not collide.
      if (!have) return true;
      return have === wantWorkerId;
    }
    if (opts?.excludeWorkerStream && m.workerStream) return false;
    return true;
  };
  if (streamId) {
    for (let i = items.length - 1; i >= 0; i--) {
      const it = items[i];
      if (it.kind === "msg" && it.msg.role === "user") break;
      // Tool activity is a hard phase fence — never resume a bubble above it,
      // even when stream_id matches (post-tool deltas start a new segment).
      if (it.kind === "card" || it.kind === "tool_prep") {
        return -1;
      }
      if (it.kind !== "msg") continue;
      const m = (it as { kind: "msg"; msg: Msg }).msg;
      if (
        m.role === "assistant"
        && m.streaming
        && m.stream_id === streamId
        && matchesStreamAffinity(m)
      ) {
        return i;
      }
    }
    return -1;
  }
  for (let i = items.length - 1; i >= 0; i--) {
    const it = items[i];
    // Tool activity is a hard phase fence — never resume a bubble above it.
    if (it.kind === "card" || it.kind === "tool_prep") {
      return -1;
    }
    // A steer row is the user's note, applied at the next safe boundary; the
    // pilot's sentence still streams into the open bubble above it.
    if (
      it.kind === "thinking"
      || it.kind === "codegraph_context"
      || it.kind === "vault_cite"
      || it.kind === "steer"
    ) {
      continue;
    }
    if (it.kind === "msg") {
      const m = (it as { kind: "msg"; msg: Msg }).msg;
      if (
        m.role === "assistant"
        && m.streaming
        && matchesStreamAffinity(m)
      ) {
        return i;
      }
      // Affinity miss on a still-open assistant (e.g. trailing worker preview
      // while looking for the pilot): keep scanning. Sealed / user msgs still
      // end the scan so we never resume under a finished bubble.
      if (m.role === "assistant" && m.streaming) {
        continue;
      }
    }
    break;
  }
  return -1;
}

/** Append decoded text to the streaming assistant bubble (pure). */
export function appendStreamingTextToItems(
  items: Item[],
  chunk: string,
  opts?: {
    isPlan?: boolean;
    streamId?: string;
    channel?: string;
    workerStream?: boolean;
    workerId?: string;
  },
): Item[] {
  if (!chunk) return items;
  const streamId = (opts?.streamId || "").trim();
  const workerStream = Boolean(opts?.workerStream);
  const workerId = (opts?.workerId || "").trim();
  const idx = findStreamingBubbleIdx(items, {
    streamId: streamId || undefined,
    ...(workerStream
      ? { workerStreamOnly: true, workerId: workerId || undefined }
      : { excludeWorkerStream: true }),
  });
  if (idx >= 0) {
    const bubble = items[idx] as { kind: "msg"; msg: Msg };
    const updated = [...items];
    const stampWorkerId =
      workerStream && workerId && !(bubble.msg.worker_id || "").trim();
    const nextText = workerStream
      ? (isTrivialAssistantCrumb(chunk) ? bubble.msg.text : sanitizeThinkingStatusGlue(bubble.msg.text + chunk))
      : absorbOpenPilotDelta(bubble.msg.text, chunk);
    updated[idx] = {
      kind: "msg",
      msg: {
        ...bubble.msg,
        text: nextText,
        ...(stampWorkerId ? { worker_id: workerId } : {}),
      },
    };
    return updated;
  }
  // cursor_gap / ring_miss replay after durable hydrate: never open a second
  // bubble for prose that already landed in a sealed assistant row.
  // Worker previews are ephemeral and must not be suppressed by pilot cover.
  if (!workerStream && sealedAssistantCoversDelta(items, chunk)) {
    return items;
  }
  return [
    ...items,
    {
      kind: "msg",
      msg: {
        role: "assistant",
        text: workerStream ? sanitizeThinkingStatusGlue(chunk) : chunk,
        streaming: true,
        isPlan: opts?.isPlan,
        ...(workerStream ? { workerStream: true } : {}),
        ...(workerStream && workerId ? { worker_id: workerId } : {}),
        ...(streamId ? { stream_id: streamId } : {}),
        ...(opts?.channel ? { channel: opts.channel } : {}),
      },
    },
  ];
}

/**
 * Seal the open pilot streaming bubble in place so a later phase (thinking /
 * tool card) cannot re-parent or reopen it. Empty / markdown-punctuation
 * crumbs are dropped so they cannot fence Sol word-sized thinking deltas.
 * Worker-stream previews are left alone (ephemeral; action_result drops them).
 */
export function finalizeOpenPilotBubble(items: Item[]): Item[] {
  const idx = findStreamingBubbleIdx(items, { excludeWorkerStream: true });
  if (idx < 0) return items;
  const bubble = items[idx] as { kind: "msg"; msg: Msg };
  if (isTrivialAssistantCrumb(bubble.msg.text || "")) {
    return [...items.slice(0, idx), ...items.slice(idx + 1)];
  }
  const updated = [...items];
  updated[idx] = {
    kind: "msg",
    msg: { ...bubble.msg, streaming: false },
  };
  return updated;
}

/**
 * Velocity-limited reveal with catch-up. Drain-all painted provider bursts
 * at whatever size they arrived; a fixed /8 drip made Kimi look like spurts.
 * Small backlogs stay near a floor; large backlogs accelerate up to a cap.
 * ``done`` flushes the remainder so the last tokens do not trail the seal.
 */
export const TYPEWRITER_MIN_CHARS = 4;
export const TYPEWRITER_MAX_CHARS = 96;
export const TYPEWRITER_CATCHUP_DIVISOR = 6;

export function typewriterCharsPerFrame(bufLen: number, done: boolean): number {
  if (bufLen <= 0) return 0;
  if (done) return bufLen;
  const catchup = Math.ceil(bufLen / TYPEWRITER_CATCHUP_DIVISOR);
  const paced = Math.max(TYPEWRITER_MIN_CHARS, Math.min(TYPEWRITER_MAX_CHARS, catchup));
  return Math.min(bufLen, paced);
}
