/**
 * Durable transcript row identities.
 *
 * Live streaming and disk hydrate mint different object references. React keys
 * must survive that handoff (`msgId#blockId`) so virtual rows do not remount.
 * Optimistic user echoes use the same id the backend persists as `input_id`.
 */

import type { Item, Msg } from "../TranscriptList";

export const TRANSCRIPT_BODY_BLOCK_ID = "body";
export const TRANSCRIPT_PROGRESS_BLOCK_ID = "progress";
export const TRANSCRIPT_WORKER_BLOCK_ID = "worker";
export const TRANSCRIPT_IDENTITY_SEP = "#";
export const TRANSCRIPT_DRAFT_SESSION_ID = "_draft";

export function allocateOptimisticInputId(): string {
  const cryptoObj = globalThis.crypto;
  if (cryptoObj && typeof cryptoObj.randomUUID === "function") {
    return cryptoObj.randomUUID().replace(/-/g, "");
  }
  return `in${Date.now().toString(16)}${Math.random().toString(16).slice(2, 10)}`;
}

export function transcriptSessionScope(sessionId?: string | null): string {
  const trimmed = String(sessionId || "").trim();
  return trimmed || TRANSCRIPT_DRAFT_SESSION_ID;
}

export function ordinalMessageId(
  sessionId: string | null | undefined,
  role: Msg["role"],
  ordinal: number,
): string {
  return `msg:${transcriptSessionScope(sessionId)}:${role}:${ordinal}`;
}

export function durableMessageId(opts: {
  explicitId?: string | null;
  inputId?: string | null;
  sessionId?: string | null;
  role: Msg["role"];
  ordinal: number;
}): string {
  const explicit = String(opts.explicitId || "").trim();
  if (explicit) return explicit;
  const inputId = String(opts.inputId || "").trim();
  if (inputId) return inputId;
  return ordinalMessageId(opts.sessionId, opts.role, opts.ordinal);
}

export function messageBlockId(msg: Pick<Msg, "workerStream" | "channel">): string {
  if (msg.workerStream) return TRANSCRIPT_WORKER_BLOCK_ID;
  if (msg.channel === "progress") return TRANSCRIPT_PROGRESS_BLOCK_ID;
  return TRANSCRIPT_BODY_BLOCK_ID;
}

export function transcriptBlockRowId(msgId: string, blockId: string): string {
  return `${msgId}${TRANSCRIPT_IDENTITY_SEP}${blockId}`;
}

export function countRoleMessages(
  items: readonly Item[],
  role: Msg["role"],
): number {
  let count = 0;
  for (const item of items) {
    if (item.kind === "msg" && item.msg.role === role && !item.msg.workerStream) count += 1;
  }
  return count;
}

// Worker previews are ephemeral and dropped when their action settles, so they
// take no ordinal (which would shift every later message's id) and get an id
// of their own per worker.
function workerPreviewId(msg: Msg, sessionId?: string | null): string {
  return `${sessionId || "session"}:worker:${msg.worker_id || "anon"}:${msg.stream_id || ""}`;
}

export function stampMessageIdentity(
  msg: Msg,
  ordinal: number,
  sessionId?: string | null,
): Msg {
  if (msg.id) return msg;
  if (msg.workerStream) return { ...msg, id: workerPreviewId(msg, sessionId) };
  return {
    ...msg,
    id: durableMessageId({
      sessionId,
      role: msg.role,
      ordinal,
    }),
  };
}

/** Stamp every message that still lacks a durable id (hydrate / live mint). */
export function stampTranscriptMessageIds(
  items: Item[],
  sessionId?: string | null,
): Item[] {
  let userOrdinal = 0;
  let assistantOrdinal = 0;
  let changed = false;
  const next = items.map((item) => {
    if (item.kind !== "msg") return item;
    const ordinal = item.msg.workerStream ? -1
      : item.msg.role === "user" ? userOrdinal++ : assistantOrdinal++;
    if (item.msg.id) return item;
    changed = true;
    return {
      ...item,
      msg: stampMessageIdentity(item.msg, ordinal, sessionId),
    };
  });
  return changed ? next : items;
}

export function withLiveMessageId(
  items: readonly Item[],
  msg: Msg,
  sessionId?: string | null,
): Msg {
  if (msg.id) return msg;
  return stampMessageIdentity(
    msg,
    countRoleMessages(items, msg.role),
    sessionId,
  );
}

export function optimisticUserEchoMsg(opts: {
  text: string;
  images?: Msg["images"];
  documents?: Msg["documents"];
  id: string;
}): Msg {
  return {
    role: "user",
    text: opts.text,
    images: opts.images,
    ...(opts.documents?.length ? { documents: opts.documents } : {}),
    id: opts.id,
  };
}
