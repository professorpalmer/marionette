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
      ? {
          kind: "thinking" as const,
          text: it.text,
          id: it.id || newThinkingId(),
          ...(it.stream_id ? { stream_id: it.stream_id } : {}),
        }
      : it
  );
}

/**
 * Empty/whitespace or markdown-formatting-only assistant crumbs (Sol dual-
 * channel `*` / `**` / `****` markers). Narrow on purpose: non-Latin prose
 * and meaningful symbol-only text remain real narration fences.
 */
export function isTrivialAssistantCrumb(text: string): boolean {
  const t = (text || "").trim();
  if (!t) return true;
  // Emphasis / code-fence crumbs only — not "→", "…", or CJK/etc. prose.
  return /^[*_`~]+$/.test(t);
}

/**
 * Merge a cumulative snapshot frame into an existing thinking row.
 * Identical / stale-prefix / strict-extension snapshots replace once;
 * non-overlapping fragments still append. Used only by non-delta
 * (`appendNonStreamingThinking`) paths — never for provider `delta:true`
 * chunks, which must keep strict append semantics.
 */
export function coalesceThinkingChunk(existing: string, chunk: string): string {
  if (!chunk) return existing;
  if (!existing) return chunk;
  if (chunk === existing) return existing;
  if (existing.startsWith(chunk)) return existing;
  if (chunk.startsWith(existing)) return chunk;
  return existing + chunk;
}

export type UpsertStreamingThinkingOpts = {
  /**
   * When true, treat `chunk` as a cumulative snapshot (ring/replay).
   * Default false: strict append for live `delta:true` provider chunks so
   * repeated or prefix-looking deltas are never dropped.
   */
  coalesceSnapshots?: boolean;
  /** Provider output-item identity — keys the surface across interleaved channels. */
  streamId?: string;
};

function mergeThinkingText(
  existing: string,
  chunk: string,
  coalesceSnapshots: boolean,
): string {
  if (coalesceSnapshots) return coalesceThinkingChunk(existing, chunk);
  if (!chunk) return existing;
  if (!existing) return chunk;
  return existing + chunk;
}

function turnStartIndex(items: Item[]): number {
  for (let i = items.length - 1; i >= 0; i--) {
    const it = items[i];
    if (it.kind === "msg" && it.msg.role === "user") return i + 1;
  }
  return 0;
}

/**
 * Append/update the open streaming reasoning row for the current turn.
 * When `streamId` is present, identity owns the surface — interleaved
 * assistant/progress deltas never mint a new REASONING row. Tool/prep cards
 * are a hard fence: post-tool deltas for the same stream_id open a NEW
 * reasoning segment at the end (append-only). Seal only via explicit
 * lifecycle helpers (item done / tool start / terminal).
 */
export function upsertStreamingThinking(
  items: Item[],
  chunk: string,
  opts?: UpsertStreamingThinkingOpts,
): Item[] {
  const coalesceSnapshots = Boolean(opts?.coalesceSnapshots);
  const streamId = (opts?.streamId || "").trim();
  const turnStart = turnStartIndex(items);

  if (streamId) {
    for (let i = items.length - 1; i >= turnStart; i--) {
      const it = items[i];
      // Never resume a reasoning row that sits above a tool boundary.
      if (it.kind === "card" || it.kind === "tool_prep") {
        break;
      }
      if (it.kind === "thinking" && it.stream_id === streamId) {
        const copy = items.slice();
        copy[i] = {
          kind: "thinking",
          text: mergeThinkingText(it.text, chunk, coalesceSnapshots),
          streaming: true,
          id: it.id || newThinkingId(),
          stream_id: streamId,
        };
        return copy;
      }
    }
    return [
      ...items,
      {
        kind: "thinking",
        text: chunk,
        streaming: true,
        id: newThinkingId(),
        stream_id: streamId,
      },
    ];
  }

  // Legacy path (no stream identity): append into the open streaming row, or
  // start a new one after a committed card / substantive assistant fence.
  for (let i = items.length - 1; i >= turnStart; i--) {
    const it = items[i];
    if (
      it.kind === "card"
      || (it.kind === "msg" && it.msg.role === "assistant" && !it.msg.workerStream)
    ) {
      if (
        it.kind === "msg"
        && isTrivialAssistantCrumb(it.msg.text || "")
      ) {
        continue;
      }
      // Substantive assistant (open or sealed) fences identity-less thinking so
      // a later reasoning phase starts a new row. Trivial crumbs are skipped
      // above so markdown markers cannot mint one REASONING header per word.
      const sealed = finalizeStreamingThinking(items);
      return [
        ...sealed,
        { kind: "thinking", text: chunk, streaming: true, id: newThinkingId() },
      ];
    }
    if (it.kind === "thinking") {
      const copy = items.slice();
      copy[i] = {
        kind: "thinking",
        text: mergeThinkingText(it.text, chunk, coalesceSnapshots),
        streaming: true,
        id: it.id || newThinkingId(),
        ...(it.stream_id ? { stream_id: it.stream_id } : {}),
      };
      return copy;
    }
  }
  return [
    ...items,
    { kind: "thinking", text: chunk, streaming: true, id: newThinkingId() },
  ];
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

/** True when `it` is a sealed assistant that looks like a finished answer. */
function isSealedFinalAssistant(it: Item): boolean {
  return (
    it.kind === "msg"
    && it.msg.role === "assistant"
    && !it.msg.streaming
    && !it.msg.workerStream
    && looksLikeFinalAnswer(it.msg.text || "")
  );
}

/**
 * Hydrate/replay cleanup only: move investigation rows (tool cards, reasoning,
 * tool_prep chrome) that landed after a trailing sealed final-looking
 * assistant to just before that assistant — preserving their relative order.
 *
 * Must NOT run on the live streaming path. Mid-turn application is strictly
 * append-only; reordering already-rendered items causes visible jumps once
 * surfaces are identity-keyed. Call only from end-of-turn / hydrate cleanup
 * that cannot fire while a turn is still streaming.
 */
export function hoistCardsBeforeTrailingFinals(items: Item[]): Item[] {
  let turnStart = 0;
  for (let i = items.length - 1; i >= 0; i--) {
    const it = items[i];
    if (it.kind === "msg" && it.msg.role === "user") {
      turnStart = i + 1;
      break;
    }
  }

  // Rightmost sealed final in the turn that still has later investigation.
  let finalIdx = -1;
  for (let i = items.length - 1; i >= turnStart; i--) {
    if (!isSealedFinalAssistant(items[i])) continue;
    let investigationAfter = false;
    for (let j = i + 1; j < items.length; j++) {
      const later = items[j];
      if (later.kind === "msg" && later.msg.role === "user") break;
      if (later.kind === "card" || later.kind === "thinking") {
        investigationAfter = true;
        break;
      }
    }
    if (investigationAfter) {
      finalIdx = i;
      break;
    }
  }
  if (finalIdx < 0) return items;

  const head = items.slice(0, finalIdx);
  const finalItem = items[finalIdx];
  const hoist: Item[] = [];
  let hoistPrep: Extract<Item, { kind: "tool_prep" }> | null = null;
  const tail: Item[] = [];
  for (let j = finalIdx + 1; j < items.length; j++) {
    const it = items[j];
    if (it.kind === "msg" && it.msg.role === "user") {
      tail.push(...items.slice(j));
      break;
    }
    if (it.kind === "card" || it.kind === "thinking") {
      hoist.push(it);
    } else if (it.kind === "tool_prep") {
      hoistPrep = it;
    } else {
      tail.push(it);
    }
  }
  if (hoist.length === 0) return items;
  return [
    ...head,
    ...hoist,
    ...(hoistPrep ? [hoistPrep] : []),
    finalItem,
    ...tail.filter((it) => it.kind !== "tool_prep"),
  ];
}

/**
 * Insert index for a new tool/prep card: always append after every existing
 * item in the turn. Live streaming is chronological / append-only — never
 * leapfrog sealed finals or insert above already-rendered narration.
 */
function toolPrepInsertIndex(items: Item[], _turnStart: number): number {
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
      return [
        ...next,
        { kind: "tool_prep" as const, name: kind },
      ];
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
    return [
      ...next,
      { kind: "tool_prep" as const, name: kind },
    ];
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

/** Seal a single stream surface by provider stream_id (item-done barrier). */
export function sealStreamById(items: Item[], streamId: string): Item[] {
  const sid = (streamId || "").trim();
  if (!sid) return items;
  const next: Item[] = [];
  for (const it of items) {
    if (it.kind === "thinking" && it.stream_id === sid && it.streaming) {
      next.push({
        kind: "thinking",
        text: it.text,
        id: it.id || newThinkingId(),
        stream_id: sid,
      });
      continue;
    }
    if (
      it.kind === "msg"
      && it.msg.role === "assistant"
      && it.msg.stream_id === sid
      && it.msg.streaming
      && !it.msg.workerStream
    ) {
      if (isTrivialAssistantCrumb(it.msg.text || "")) continue;
      next.push({ kind: "msg", msg: { ...it.msg, streaming: false } });
      continue;
    }
    next.push(it);
  }
  return next;
}
