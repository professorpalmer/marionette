import type { Card, Item, Msg } from "../TranscriptList";
import {
  clearToolPrepPlaceholders,
  finalizeStreamingThinking,
  newThinkingId,
} from "./thinkingToolPrep";
import { findStreamingBubbleIdx } from "./streamBubbles";
import { deduplicateConsecutiveAssistantMessages } from "./transcriptItems";

/** Patch a tool card by id (investigation / action_result path). */
export function patchCardInItems(
  items: Item[],
  id: string,
  patch: Partial<Card>,
): Item[] {
  return items.map((it) => {
    if (it.kind === "card" && it.card.id === id) {
      return { kind: "card", card: { ...it.card, ...patch } };
    }
    return it;
  });
}

/** Deduped auth_failure banner (swarm_auth_failure or action_result fallback). */
export function appendAuthFailure(
  items: Item[],
  message: string,
  id?: string,
): Item[] {
  if (items.some((it) => it.kind === "auth_failure" && it.id === id)) {
    return items;
  }
  return [...items, { kind: "auth_failure" as const, message, id }];
}

export function appendCommandBlocked(
  items: Item[],
  d: { command?: string; category?: string; reason?: string; matched?: string },
): Item[] {
  return [
    ...items,
    {
      kind: "command_blocked" as const,
      command: d.command || "",
      category: d.category || "",
      reason: d.reason || "",
      matched: d.matched || "",
    },
  ];
}

export function appendCodegraphContext(
  items: Item[],
  symbols: number,
  query: string,
): Item[] {
  return [...items, { kind: "codegraph_context" as const, symbols, query }];
}

export function appendCompaction(
  items: Item[],
  beforeTokens: number,
  afterTokens: number,
): Item[] {
  return [
    ...items,
    {
      kind: "compaction" as const,
      before_tokens: beforeTokens,
      after_tokens: afterTokens,
    },
  ];
}

/** Truncate wait/notice hints for the composer footer (72 → "70…"). */
export function truncateWaitHint(message: string): string | null {
  const msg = String(message || "").trim();
  if (!msg) return null;
  return msg.length > 72 ? `${msg.slice(0, 70)}…` : msg;
}

/** Whether a thinking SSE frame should paint (live delta vs post-answer dump). */
export function shouldPaintThinking(d: {
  text?: unknown;
  delta?: unknown;
}): { painting: boolean; chunk: string } {
  const chunk = String(d.text || "");
  const painting = Boolean(d.delta) ? Boolean(chunk) : Boolean(chunk.trim());
  return { painting, chunk };
}

/** Ensure an open pilot streaming bubble exists (message_delta path). */
export function ensureAssistantStreamingBubble(
  items: Item[],
  opts?: { isPlan?: boolean },
): Item[] {
  const base = finalizeStreamingThinking(items);
  if (findStreamingBubbleIdx(base) >= 0) return base;
  return [
    ...base,
    {
      kind: "msg",
      msg: {
        role: "assistant",
        text: "",
        streaming: true,
        isPlan: opts?.isPlan,
      },
    },
  ];
}

/**
 * Ensure a workerStream preview bubble exists. Never merges into the pilot
 * bubble; reuses only a trailing workerStream streaming msg.
 */
export function ensureWorkerStreamingBubble(
  items: Item[],
  opts?: { isPlan?: boolean },
): Item[] {
  const lastIdx = items.length - 1;
  if (lastIdx >= 0 && items[lastIdx].kind === "msg") {
    const lastMsg = items[lastIdx] as { kind: "msg"; msg: Msg };
    if (
      lastMsg.msg.role === "assistant"
      && lastMsg.msg.streaming
      && lastMsg.msg.workerStream
    ) {
      return items;
    }
  }
  return [
    ...items,
    {
      kind: "msg",
      msg: {
        role: "assistant",
        text: "",
        streaming: true,
        workerStream: true,
        isPlan: opts?.isPlan,
      },
    },
  ];
}

/** Finalize pilot `message` event into transcript items. */
export function finalizePilotMessage(
  items: Item[],
  text: string | undefined,
  opts?: { isPlan?: boolean },
): Item[] {
  // Drop trailing worker-stream preview before finalizing the pilot's own text.
  const p = finalizeStreamingThinking(
    items.length > 0
      && items[items.length - 1].kind === "msg"
      && (items[items.length - 1] as { kind: "msg"; msg: Msg }).msg.workerStream
      ? items.slice(0, -1)
      : items,
  );
  const streamIdx = findStreamingBubbleIdx(p, { excludeWorkerStream: true });
  if (streamIdx >= 0) {
    const lastMsg = p[streamIdx] as Extract<Item, { kind: "msg" }>;
    const finalText = text || lastMsg.msg.text || "";
    if (!finalText.trim()) {
      return [...p.slice(0, streamIdx), ...p.slice(streamIdx + 1)];
    }
    const updatedItems = [...p];
    updatedItems[streamIdx] = {
      kind: "msg",
      msg: { ...lastMsg.msg, text: finalText, streaming: false },
    };
    return deduplicateConsecutiveAssistantMessages(updatedItems);
  }
  if (!text) return p;
  return deduplicateConsecutiveAssistantMessages([
    ...p,
    {
      kind: "msg",
      msg: { role: "assistant", text: text || "", isPlan: opts?.isPlan },
    },
  ]);
}

/** Idempotent action_start card append (session-switch SSE race safe). */
export function appendActionStartCard(
  items: Item[],
  d: { id: string; goal?: string; cwd?: string | null; kind?: string },
): Item[] {
  const base = clearToolPrepPlaceholders(finalizeStreamingThinking(items));
  if (base.some((it) => it.kind === "card" && it.card.id === d.id)) return base;
  return [
    ...base,
    {
      kind: "card",
      card: {
        id: d.id,
        // Match prior Conversation wiring: goal may be undefined on the wire.
        goal: d.goal as string,
        cwd: d.cwd,
        running: true,
        open: false,
        kind: d.kind,
      },
    },
  ];
}

/**
 * On action_result: drop ephemeral worker preview, or finalize a non-empty
 * pilot streaming bubble in place.
 */
export function finalizeStreamingBubbleOnActionResult(items: Item[]): Item[] {
  const lastIdx = items.length - 1;
  if (lastIdx >= 0 && items[lastIdx].kind === "msg") {
    const lastMsg = items[lastIdx] as { kind: "msg"; msg: Msg };
    if (lastMsg.msg.role === "assistant" && lastMsg.msg.streaming) {
      const finalText = (lastMsg.msg.text || "").trim();
      if (lastMsg.msg.workerStream) {
        return items.slice(0, lastIdx);
      }
      if (!finalText) {
        return items.slice(0, lastIdx);
      }
      const updated = [...items];
      updated[lastIdx] = {
        kind: "msg",
        msg: { ...lastMsg.msg, streaming: false },
      };
      return updated;
    }
  }
  return items;
}

/** Prefer resolved path from action_result over card.goal for relocate events. */
export function workspaceRootFromActionResult(
  d: { workspace_root?: unknown; path?: unknown; repo?: unknown },
  cardGoal?: string,
): string {
  return String(d.workspace_root || d.path || d.repo || cardGoal || "").trim();
}

export function appendSwarmPending(
  items: Item[],
  jobIds: string[],
  objective: string,
): Item[] {
  return [
    ...items,
    {
      kind: "swarm_pending" as const,
      job_ids: jobIds,
      objective,
      resolved: false,
    },
  ];
}

export function appendCheckpoint(
  items: Item[],
  d: { id?: string; label?: string; trigger?: string },
): Item[] {
  return [
    ...items,
    {
      kind: "checkpoint" as const,
      id: d.id as string,
      label: d.label as string,
      trigger: d.trigger as string,
    },
  ];
}

export function appendQueuedPromptUserBubble(
  items: Item[],
  text: string,
  images: string[] = [],
): Item[] {
  const bubbleImgs = images.map((p: string) => ({
    path: p,
    name: (p.split(/[\\/]/).pop() || p),
    previewUrl: p,
  }));
  return [
    ...items,
    { kind: "msg", msg: { role: "user", text, images: bubbleImgs } },
  ];
}

export function appendAutoHalt(items: Item[], reason: string): Item[] {
  return [
    ...items,
    { kind: "msg", msg: { role: "assistant", text: "HALT: " + (reason || "") } },
  ];
}

export function appendStreamError(items: Item[], error: string): Item[] {
  return [
    ...items,
    {
      kind: "msg",
      msg: { role: "assistant", text: "[error] " + (error || "") },
    },
  ];
}

export function appendNonStreamingThinking(items: Item[], text: string): Item[] {
  return [...items, { kind: "thinking", text, id: newThinkingId() }];
}

/**
 * Resolve a swarm_result into transcript items: mark matching pending chips
 * resolved and append the result row (idempotent by job_id).
 */
export function applySwarmResultToItems(
  items: Item[],
  d: {
    job_id?: string;
    objective?: string;
    result?: any;
    applied?: boolean;
    files?: string[];
    summary?: string;
    error?: string | null;
  },
): Item[] {
  const jobId = d.job_id;
  if (!jobId) return items;

  const pendingItem = items.find(
    (it) => it.kind === "swarm_pending" && it.job_ids.includes(jobId),
  );
  const pendingObj =
    pendingItem && pendingItem.kind === "swarm_pending"
      ? pendingItem.objective
      : "";
  const finalObjective = d.objective || pendingObj || "";

  const updated = items.map((item) => {
    if (item.kind === "swarm_pending" && item.job_ids.includes(jobId)) {
      return { ...item, resolved: true };
    }
    return item;
  });

  if (updated.some((it) => it.kind === "swarm_result" && it.job_id === jobId)) {
    return updated;
  }

  const resObj = d.result || d;
  return [
    ...updated,
    {
      kind: "swarm_result" as const,
      job_id: jobId,
      applied: resObj.applied,
      files: resObj.files || [],
      summary: resObj.summary || "",
      error: resObj.error || null,
      objective: finalObjective,
    },
  ];
}

/** Self-learning footnote copy; null when nothing actionable was proposed. */
export function formatDistilledNotice(d: {
  skill?: { status?: string; name?: string };
  rules?: { proposed?: unknown[] };
}): string | null {
  const parts: string[] = [];
  if (d.skill && d.skill.status === "proposed") {
    const { name } = d.skill;
    parts.push(`proposed 1 skill${name ? ` ("${name}")` : ""}`);
  }
  if (d.rules) {
    const pCount = d.rules.proposed?.length || 0;
    if (pCount > 0) {
      parts.push(`proposed ${pCount} rule${pCount === 1 ? "" : "s"}`);
    }
  }
  if (parts.length === 0) return null;
  return `Self-learning: ${parts.join(", ")} - review in Skills tab`;
}

export function formatWikiAutoIngestNotice(pageCount: number): string {
  return `Wiki: ${pageCount} page${pageCount === 1 ? "" : "s"} auto-ingested (local orchestration)`;
}
