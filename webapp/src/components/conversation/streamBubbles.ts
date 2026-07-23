import type { Item, Msg } from "../TranscriptList";
import { isTrivialAssistantCrumb } from "./thinkingToolPrep";

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
 * Skip ephemeral decoration that may land while the typewriter drains
 * (thinking rows, codegraph chips). Do NOT scan past tool/prep cards: once a
 * card exists after an assistant bubble, later deltas must open a post-card
 * bubble rather than resume pre-tool narration (Cursor CLI/ACP investigation).
 *
 * When excludeWorkerStream is set, skip ephemeral swarm worker preview bubbles
 * so the pilot's open bubble is finalized instead.
 */
export function findStreamingBubbleIdx(
  items: Item[],
  opts?: { excludeWorkerStream?: boolean },
): number {
  for (let i = items.length - 1; i >= 0; i--) {
    const it = items[i];
    // Tool activity is a hard phase fence — never resume a bubble above it.
    if (it.kind === "card" || it.kind === "tool_prep") {
      return -1;
    }
    if (it.kind === "thinking" || it.kind === "codegraph_context") {
      continue;
    }
    if (it.kind === "msg") {
      const m = (it as { kind: "msg"; msg: Msg }).msg;
      if (
        m.role === "assistant"
        && m.streaming
        && (!opts?.excludeWorkerStream || !m.workerStream)
      ) {
        return i;
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
  opts?: { isPlan?: boolean },
): Item[] {
  if (!chunk) return items;
  const idx = findStreamingBubbleIdx(items);
  if (idx >= 0) {
    const bubble = items[idx] as { kind: "msg"; msg: Msg };
    const updated = [...items];
    updated[idx] = {
      kind: "msg",
      msg: { ...bubble.msg, text: bubble.msg.text + chunk },
    };
    return updated;
  }
  // cursor_gap / ring_miss replay after durable hydrate: never open a second
  // bubble for prose that already landed in a sealed assistant row.
  if (sealedAssistantCoversDelta(items, chunk)) {
    return items;
  }
  return [
    ...items,
    {
      kind: "msg",
      msg: {
        role: "assistant",
        text: chunk,
        streaming: true,
        isPlan: opts?.isPlan,
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
 * Typewriter drain rate: scale with backlog so live streams never lag
 * arbitrarily far behind; accelerate when the stream has ended.
 */
export function typewriterCharsPerFrame(bufLen: number, done: boolean): number {
  if (bufLen <= 0) return 0;
  return done
    ? Math.max(12, Math.ceil(bufLen / 4))
    : Math.max(3, Math.ceil(bufLen / 8));
}
