import { normalizeToolKind } from "../../lib/turnProgress";
import type { Item } from "../TranscriptList";

let thinkingIdSeq = 0;
function newThinkingId(): string {
  thinkingIdSeq += 1;
  return `th-${Date.now().toString(36)}-${thinkingIdSeq}`;
}

/** Drop streaming:true from live reasoning rows once the phase ends. */
export function finalizeStreamingThinking(items: Item[]): Item[] {
  return items.map((it) =>
    it.kind === "thinking" && it.streaming
      ? { kind: "thinking" as const, text: it.text, id: it.id || newThinkingId() }
      : it
  );
}

/** Append/update the open streaming reasoning row for the current turn.
 * Preserves a durable `id` across token upserts so the ActivityGroup React key
 * (and expand/scroll state) does not remount on every thinking delta. */
export function upsertStreamingThinking(items: Item[], chunk: string): Item[] {
  for (let i = items.length - 1; i >= 0; i--) {
    const it = items[i];
    if (it.kind === "msg" && it.msg.role === "user") break;
    if (it.kind === "thinking" && it.streaming) {
      const copy = items.slice();
      copy[i] = {
        kind: "thinking",
        text: it.text + chunk,
        streaming: true,
        id: it.id || newThinkingId(),
      };
      return copy;
    }
  }
  return [...items, { kind: "thinking", text: chunk, streaming: true, id: newThinkingId() }];
}

export type ToolPrepOpts = {
  /** Path / command / query for the row (Cursor ACP locations / args). */
  goal?: string;
  /** Stable Cursor toolCallId / stream call_id — accumulate one row per id. */
  id?: string;
  /** pending | in_progress | completed | failed | cancelled */
  status?: string;
};

/** Upsert a provisional running card for tool_prep so ActivityGroup appears
 * as soon as tools start. Cursor ACP/CLI pass call ids so each native tool
 * keeps its own row; legacy string-only hints still replace the anonymous
 * placeholder (pre-action_start). */
export function upsertToolPrep(
  items: Item[],
  name: string,
  opts?: ToolPrepOpts,
): Item[] {
  const callId = (opts?.id || "").trim();
  const status = (opts?.status || "").toLowerCase().trim();
  const done =
    status === "completed" || status === "failed" || status === "cancelled";
  const kind = normalizeToolKind(name) || (name || "").trim() || "tool_call";
  // Real path/command/query only — never echo the kind as the goal
  // ("Read" + "read file" / "Tool" + "tool" painted as doubled chrome).
  const goalRaw = (opts?.goal || "").trim();
  const prepId = callId ? `tool-prep:${callId}` : `tool-prep:${kind}`;

  let lastUser = -1;
  for (let i = items.length - 1; i >= 0; i--) {
    const it = items[i];
    if (it.kind === "msg" && it.msg.role === "user") {
      lastUser = i;
      break;
    }
  }
  const turnStart = lastUser + 1;

  // Status-only / completed patch for a known call — never clobber a path
  // goal with the bare kind label ("read file").
  if (callId && done) {
    let hit = false;
    const patched = items.map((it, i) => {
      if (i < turnStart || it.kind !== "card") return it;
      if (it.card.id !== prepId) return it;
      hit = true;
      return {
        ...it,
        card: {
          ...it.card,
          running: false,
          kind: (kind !== "tool_call" ? kind : it.card.kind) || kind,
          goal: goalRaw || it.card.goal || "",
          ...(status === "failed"
            ? { result: { ...(it.card.result || {}), error: "failed" } }
            : {}),
        },
      };
    });
    if (hit) {
      return patched.filter((it, i) => !(i >= turnStart && it.kind === "tool_prep"));
    }
  }

  const card = {
    id: prepId,
    goal: goalRaw,
    cwd: null as string | null,
    kind,
    running: !done,
    open: false,
    ...(status === "failed" ? { result: { error: "failed" } } : {}),
  };

  // With a stable call id: keep prior Cursor tool rows; update matching id.
  if (callId) {
    let replaced = false;
    const next = items.map((it, i) => {
      if (i < turnStart) return it;
      if (it.kind === "tool_prep") return it; // drop below
      if (it.kind === "card" && it.card.id === prepId) {
        replaced = true;
        return {
          kind: "card" as const,
          card: {
            ...it.card,
            ...card,
            goal: goalRaw || it.card.goal || "",
            kind: kind !== "tool_call" ? kind : (it.card.kind || kind),
            running: card.running,
          },
        };
      }
      return it;
    }).filter((it, i) => !(i >= turnStart && it.kind === "tool_prep"));
    if (replaced) {
      return [...next, { kind: "tool_prep" as const, name: kind }];
    }
    return [
      ...next,
      { kind: "card" as const, card },
      { kind: "tool_prep" as const, name: kind },
    ];
  }

  // Legacy string-only hint: replace anonymous prep cards in this turn.
  const next = items.filter((it, i) => {
    if (i < turnStart) return true;
    if (it.kind === "tool_prep") return false;
    if (
      it.kind === "card"
      && typeof it.card.id === "string"
      && it.card.id.startsWith("tool-prep:")
    ) {
      return false;
    }
    return true;
  });
  return [...next, { kind: "card" as const, card }, { kind: "tool_prep" as const, name: kind }];
}

/** Strip provisional tool-prep cards (and footer hints) before a real action_start. */
export function clearToolPrepPlaceholders(items: Item[]): Item[] {
  return items.filter((it) => {
    if (it.kind === "tool_prep") return false;
    if (
      it.kind === "card"
      && typeof it.card.id === "string"
      && it.card.id.startsWith("tool-prep:")
    ) {
      return false;
    }
    return true;
  });
}
