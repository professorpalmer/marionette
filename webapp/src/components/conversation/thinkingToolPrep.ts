import { normalizeToolKind } from "../../lib/turnProgress";
import type { Item } from "../TranscriptList";

let thinkingIdSeq = 0;
/** Durable id for a thinking row (exported for non-streaming inserts in Conversation). */
export function newThinkingId(): string {
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
 * (and expand/scroll state) does not remount on every thinking delta.
 *
 * Phase barrier: never reopen or append into a thinking row that already has a
 * later assistant bubble or tool card after it — those surfaces are committed.
 * A new thinking_delta after a message/tool always APPENDs a fresh thinking row. */
export function upsertStreamingThinking(items: Item[], chunk: string): Item[] {
  for (let i = items.length - 1; i >= 0; i--) {
    const it = items[i];
    if (it.kind === "msg" && it.msg.role === "user") break;
    // Committed surfaces after an earlier thinking row: seal any still-open
    // reasoning and start a new row so content cannot jump back into thinking.
    if (
      it.kind === "card"
      || (it.kind === "msg" && it.msg.role === "assistant")
    ) {
      const sealed = finalizeStreamingThinking(items);
      return [
        ...sealed,
        { kind: "thinking", text: chunk, streaming: true, id: newThinkingId() },
      ];
    }
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

/**
 * True when sealed assistant prose looks like a finished answer rather than
 * a short pre-tool preamble ("I'll validate…").
 *
 * Cursor CLI/ACP often flushes the final readout before buffered tool_call
 * events arrive; without this, the first tool card appends *after* that
 * answer and the Explored fold renders under the summary.
 */
export function looksLikeFinalAnswer(text: string): boolean {
  const t = (text || "").trim();
  if (!t) return false;
  if (t.length >= 240) return true;
  if ((t.match(/\n/g) || []).length >= 3) return true;
  // Markdown table (audit validation summaries).
  if (/^\|.+\|$/m.test(t) && t.includes("|---")) return true;
  return false;
}

/**
 * Insert index for a new tool/prep card inside the current turn.
 * Pre-tool narration (assistant/thinking with no prior card) stays above;
 * once a card exists, later assistant/thinking rows stay below new tools.
 *
 * First card in a turn also leapfrogs trailing sealed *final-looking*
 * assistants so late Cursor tool events cannot leave Explored under the answer.
 */
function toolPrepInsertIndex(items: Item[], turnStart: number): number {
  let insertAt = items.length;
  const turnHasCard = items
    .slice(turnStart)
    .some((row) => row.kind === "card");

  if (!turnHasCard) {
    // Leapfrog only consecutive trailing finals — never short sticky preambles.
    for (let i = items.length - 1; i >= turnStart; i--) {
      const it = items[i];
      if (it.kind === "tool_prep") continue;
      if (
        it.kind === "msg"
        && it.msg.role === "assistant"
        && !it.msg.streaming
        && !it.msg.workerStream
        && looksLikeFinalAnswer(it.msg.text || "")
      ) {
        insertAt = i;
        continue;
      }
      break;
    }
    return insertAt;
  }

  // After the first tool card exists, APPEND new tools at the end of the turn.
  // Inserting before trailing assistant/thinking surfaces used to batch every
  // tool above all mid-turn narration ("functions on top, transcript on
  // bottom") and then reshuffle on seal. Chronological interleave:
  // think → tool → type → tool → type, matching event order inside the fold.
  return items.length;
}

function withToolPrepChrome(
  items: Item[],
  insertAt: number,
  card: Extract<Item, { kind: "card" }>,
  kind: string,
): Item[] {
  const head = items.slice(0, insertAt);
  const tail = items.slice(insertAt);
  return [
    ...head,
    card,
    { kind: "tool_prep" as const, name: kind },
    ...tail.filter((it) => it.kind !== "tool_prep"),
  ];
}

/** Upsert a provisional running card for tool_prep so ActivityGroup appears
 * as soon as tools start. Cursor ACP/CLI pass call ids so each native tool
 * keeps its own row; legacy string-only hints still replace the anonymous
 * placeholder (pre-action_start). Correlation is call_id-primary only —
 * never kind-only / oldest-prep matching. */
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
  // goal with the bare kind label ("read file"). Match the provisional prep
  // id OR a promoted durable card that carries the same call_id / id.
  if (callId && done) {
    let hit = false;
    const patched = items.map((it, i) => {
      if (i < turnStart || it.kind !== "card") return it;
      const card = it.card;
      const matchesPrep = card.id === prepId;
      const matchesPromoted =
        card.call_id === callId
        || card.id === callId;
      if (!matchesPrep && !matchesPromoted) return it;
      hit = true;
      const terminalResult =
        status === "failed"
          ? { ...(card.result || {}), error: "failed" }
          : status === "cancelled"
            ? { ...(card.result || {}), error: "cancelled", status: "interrupted" }
            : (card.result || (matchesPromoted ? { status: "complete" } : undefined));
      return {
        ...it,
        card: {
          ...card,
          running: false,
          call_id: card.call_id || callId,
          kind: (kind !== "tool_call" ? kind : card.kind) || kind,
          goal: goalRaw || card.goal || "",
          ...(terminalResult ? { result: terminalResult } : {}),
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
    // Stamp stable call identity on create so late completed/failed prep and
    // action_result can patch this row (or its promoted durable successor).
    ...(callId ? { call_id: callId } : {}),
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
    return withToolPrepChrome(
      next,
      toolPrepInsertIndex(next, turnStart),
      { kind: "card" as const, card },
      kind,
    );
  }

  // Legacy string-only hint: only replace the matching prep id (kind[+goal]).
  // Never wipe unrelated provisional rows — that stole Read slots for Write.
  // Never fall back to kind-only / oldest-prep across unrelated rows.
  const goalKey = goalRaw ? `:${goalRaw}` : "";
  const legacyId = `tool-prep:${kind}${goalKey}`;
  const legacyCard = { ...card, id: goalRaw ? legacyId : prepId };
  let replaced = false;
  const next = items.map((it, i) => {
    if (i < turnStart) return it;
    if (it.kind === "tool_prep") return it;
    if (it.kind === "card" && typeof it.card.id === "string") {
      if (it.card.id === legacyCard.id || (!goalRaw && it.card.id === prepId)) {
        replaced = true;
        return {
          kind: "card" as const,
          card: {
            ...it.card,
            ...legacyCard,
            goal: goalRaw || it.card.goal || "",
            kind: kind !== "tool_call" ? kind : (it.card.kind || kind),
            running: legacyCard.running,
          },
        };
      }
    }
    return it;
  }).filter((it, i) => !(i >= turnStart && it.kind === "tool_prep"));
  if (replaced) {
    return [...next, { kind: "tool_prep" as const, name: kind }];
  }
  return withToolPrepChrome(
    next,
    toolPrepInsertIndex(next, turnStart),
    { kind: "card" as const, card: legacyCard },
    kind,
  );
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
