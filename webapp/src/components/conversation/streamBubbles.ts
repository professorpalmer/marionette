import type { Item, Msg } from "../TranscriptList";

/**
 * Find the open streaming assistant bubble, scanning back past decoration
 * items (reasoning rows, tool cards, codegraph chips) that may land after it
 * while the typewriter is still draining.
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
    if (
      it.kind === "card"
      || it.kind === "thinking"
      || it.kind === "tool_prep"
      || it.kind === "codegraph_context"
    ) {
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
 * Typewriter drain rate: scale with backlog so live streams never lag
 * arbitrarily far behind; accelerate when the stream has ended.
 */
export function typewriterCharsPerFrame(bufLen: number, done: boolean): number {
  if (bufLen <= 0) return 0;
  return done
    ? Math.max(12, Math.ceil(bufLen / 4))
    : Math.max(3, Math.ceil(bufLen / 8));
}
