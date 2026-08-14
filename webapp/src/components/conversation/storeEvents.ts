/**
 * Unified session store event cursor helpers (read_events_since).
 * One cursor covers mid-turn stream frames + runners/busy chrome.
 */

import type { StoreEvent } from "../../lib/transport";
import {
  chatFrameToStreamEvent,
  isChatEventReplayMiss,
  isTerminalStreamKind,
} from "./chatEvents";

/** Poll cadence for the single store-event subscription. */
export const STORE_EVENTS_POLL_MS = 1000;

/** Advance last-applied store cursor after a read_events_since batch. */
export function nextStoreCursor(
  lastApplied: number,
  events: { id?: number }[],
  responseCursor?: number,
): number {
  let next = lastApplied;
  for (const ev of events) {
    if (typeof ev.id === "number" && ev.id > next) next = ev.id;
  }
  if (typeof responseCursor === "number" && responseCursor > next) {
    next = responseCursor;
  }
  return next;
}

/**
 * Generation + session fence for store-event apply.
 * Late events from a prior stream generation or switched session must not paint.
 */
export function shouldApplyStoreEvent(opts: {
  streamGen: number;
  subscriptionGen: number;
  cachedSessionId: string | null | undefined;
  subscriptionSid: string;
}): boolean {
  if (opts.streamGen !== opts.subscriptionGen) return false;
  if (!opts.subscriptionSid) return false;
  return opts.cachedSessionId === opts.subscriptionSid;
}

/** Map a store ``stream`` event payload to the live stream-event shape. */
export function storeStreamToStreamEvent(data: {
  kind?: string;
  data?: any;
}): { kind: string; data?: any } {
  return chatFrameToStreamEvent({
    kind: String(data?.kind || "event"),
    data: data?.data,
  });
}

/** Whether a store event batch contained a terminal stream kind. */
export function storeBatchSawTerminal(events: StoreEvent[]): boolean {
  for (const ev of events) {
    if (ev.kind !== "stream") continue;
    const kind = String(ev.data?.kind || "");
    if (isTerminalStreamKind(kind)) return true;
  }
  return false;
}

/** Extract ring_miss payload fields for existing miss helpers. */
export function storeRingMissFields(ev: StoreEvent): {
  ok?: boolean;
  missed?: boolean;
  available?: boolean;
  code?: string;
  generation?: number;
} {
  const data = ev.data && typeof ev.data === "object" ? ev.data : {};
  return {
    ok: data.ok,
    missed: data.missed,
    available: data.available,
    code: data.code,
    generation: data.generation,
  };
}

export function isStoreRingMissEvent(ev: StoreEvent): boolean {
  if (ev.kind !== "ring_miss") return false;
  return isChatEventReplayMiss(storeRingMissFields(ev));
}
