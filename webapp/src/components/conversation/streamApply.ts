import type {
  Card,
  CommandApprovalItem,
  Item,
  Msg,
  SwarmPendingItem,
  SwarmPendingStatus,
} from "../TranscriptList";
import type { AutoBudgetSnapshot } from "../../lib/autoReceipts";
import {
  clearToolPrepPlaceholders,
  finalizeStreamingThinking,
  newThinkingId,
} from "./thinkingToolPrep";
import {
  finalizeOpenPilotBubble,
  findStreamingBubbleIdx,
} from "./streamBubbles";
import { deduplicateConsecutiveAssistantMessages } from "./transcriptItems";

/**
 * Seal every open stream surface (thinking row + pilot bubble) before a new
 * phase starts. Later events may only APPEND; they must not reopen, merge, or
 * re-parent content that already painted on a prior surface.
 */
export function sealOpenStreamSurfaces(items: Item[]): Item[] {
  return finalizeOpenPilotBubble(finalizeStreamingThinking(items));
}

export function swarmPendingStatus(item: SwarmPendingItem): SwarmPendingStatus {
  if (item.status) return item.status;
  if (item.resolved) return "done";
  return "running";
}

function isSwarmPendingTerminal(item: SwarmPendingItem): boolean {
  const status = swarmPendingStatus(item);
  return status === "done" || status === "failed" || status === "ended";
}

function swarmResultLooksFailed(resObj: {
  applied?: boolean;
  error?: string | null;
}): boolean {
  return resObj.applied === false || Boolean(resObj.error);
}

function withPendingTerminal(
  item: SwarmPendingItem,
  status: "done" | "failed" | "ended",
  terminalJobIds: string[],
): SwarmPendingItem {
  return {
    ...item,
    status,
    resolved: true,
    terminal_job_ids: terminalJobIds,
  };
}

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

const COMMAND_HASH_HEX = /^[0-9a-f]{64}$/;

export function appendCommandApproval(
  items: Item[],
  data: {
    id?: string;
    command?: string;
    command_hash?: string;
    session_id?: string;
    workspace_root?: string;
    category?: string;
    reason?: string;
    matched?: string;
  },
): Item[] {
  // Reject empty/malformed hashes so they cannot occupy the empty-string
  // dedupe key and suppress later valid approval cards.
  const commandHash = (data.command_hash || "").trim().toLowerCase();
  if (!COMMAND_HASH_HEX.test(commandHash)) {
    return items;
  }
  if (items.some(
    (item) => item.kind === "command_approval" && item.commandHash === commandHash,
  )) {
    return items;
  }
  return [
    ...items,
    {
      kind: "command_approval",
      id: data.id || commandHash,
      command: data.command || "",
      commandHash,
      sessionId: data.session_id || "",
      workspaceRoot: data.workspace_root || "",
      category: data.category || "",
      reason: data.reason || "",
      matched: data.matched || "",
      status: "pending",
    },
  ];
}

export function updateCommandApproval(
  items: Item[],
  commandHash: string,
  patch: Partial<CommandApprovalItem>,
): Item[] {
  return items.map((item) => (
    item.kind === "command_approval" && item.commandHash === commandHash
      ? { ...item, ...patch, kind: "command_approval" }
      : item
  ));
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
  // Seal thinking + pilot bubbles first so tool cards never absorb prior text.
  const base = clearToolPrepPlaceholders(sealOpenStreamSurfaces(items));
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
      status: "running" as const,
      terminal_job_ids: [],
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

/** Quiet AutoBudget progress chip — replaces a trailing auto_status to avoid spam. */
export function appendAutoStatus(
  items: Item[],
  cycle: number,
  snapshot?: AutoBudgetSnapshot | null,
): Item[] {
  const next = {
    kind: "auto_status" as const,
    cycle: Number.isFinite(cycle) ? Math.max(0, Math.round(cycle)) : 0,
    snapshot: (snapshot && typeof snapshot === "object") ? snapshot : {},
  };
  const last = items[items.length - 1];
  if (last?.kind === "auto_status") {
    return [...items.slice(0, -1), next];
  }
  return [...items, next];
}

/** Terminal full-auto receipt — not an assistant chat bubble. */
export function appendAutoHalt(
  items: Item[],
  reason: string,
  snapshot?: AutoBudgetSnapshot | null,
): Item[] {
  return [
    ...items,
    {
      kind: "auto_halt" as const,
      reason: reason || "",
      snapshot: (snapshot && typeof snapshot === "object") ? snapshot : {},
    },
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
 * Resolve a swarm_result into transcript items: advance matching pending chips
 * toward a terminal state (done/failed) and append the result row (idempotent
 * by job_id). For run_parallel pills that list several ids, the chip stays
 * running until every constituent job has a terminal result.
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

  const resObj = d.result || d;
  const resultFailed = swarmResultLooksFailed(resObj);

  let pendingIdx = items.findIndex(
    (it) => it.kind === "swarm_pending" && it.job_ids.includes(jobId),
  );
  // Sync run_swarm emits pending as local-swarm-{aid} but swarm_result may
  // carry the substrate job id — fall back to objective match for a single
  // still-running pill so the spinner does not stick next to a failed card.
  if (pendingIdx < 0 && d.objective) {
    pendingIdx = items.findIndex(
      (it) =>
        it.kind === "swarm_pending"
        && !isSwarmPendingTerminal(it)
        && it.objective === d.objective
        && it.job_ids.length === 1,
    );
  }

  const pendingItem =
    pendingIdx >= 0 && items[pendingIdx].kind === "swarm_pending"
      ? (items[pendingIdx] as SwarmPendingItem)
      : null;
  const finalObjective = d.objective || pendingItem?.objective || "";

  const updated = items.map((item, idx) => {
    if (idx !== pendingIdx || item.kind !== "swarm_pending") return item;

    const creditedIds = item.job_ids.includes(jobId)
      ? [jobId]
      : item.job_ids.length === 1
        ? item.job_ids
        : [jobId];
    const terminalJobIds = [
      ...new Set([...(item.terminal_job_ids || []), ...creditedIds]),
    ];
    const allTerminal = item.job_ids.every((id) => terminalJobIds.includes(id));
    if (!allTerminal) {
      return {
        ...item,
        terminal_job_ids: terminalJobIds,
        status: "running" as const,
        resolved: false,
      };
    }

    // Any prior credited failure on this pill, or this result, flips to failed.
    const priorFailed = swarmPendingStatus(item) === "failed";
    const siblingFailed = items.some(
      (it) =>
        it.kind === "swarm_result"
        && item.job_ids.includes(it.job_id)
        && swarmResultLooksFailed(it),
    );
    const status: "done" | "failed" =
      priorFailed || siblingFailed || resultFailed ? "failed" : "done";
    return withPendingTerminal(item, status, terminalJobIds);
  });

  if (updated.some((it) => it.kind === "swarm_result" && it.job_id === jobId)) {
    return updated;
  }

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

/**
 * When a sync swarm dies with only action_result(error) (no swarm_result),
 * flip the matching local-swarm-{actionId} pill to failed.
 */
export function failSwarmPendingForActionError(
  items: Item[],
  actionId: string | undefined,
): Item[] {
  if (!actionId) return items;
  const localId = `local-swarm-${actionId}`;
  return items.map((item) => {
    if (item.kind !== "swarm_pending" || isSwarmPendingTerminal(item)) return item;
    if (!item.job_ids.includes(localId)) return item;
    return withPendingTerminal(item, "failed", [...item.job_ids]);
  });
}

/**
 * Turn-close safety net: still-spinning pills whose job ids are not live in
 * the tracker flip to a neutral "ended" state (no spinner). Also reconciles
 * pills that already have covering swarm_result rows but never flipped
 * (e.g. job_id alias mismatch).
 */
export function finalizeOrphanSwarmPills(
  items: Item[],
  liveJobIds: ReadonlySet<string> | readonly string[],
): Item[] {
  const live = liveJobIds instanceof Set ? liveJobIds : new Set(liveJobIds);

  const resultByJob = new Map<string, { failed: boolean; objective: string }>();
  for (const it of items) {
    if (it.kind !== "swarm_result") continue;
    resultByJob.set(it.job_id, {
      failed: swarmResultLooksFailed(it),
      objective: it.objective || "",
    });
  }

  return items.map((item) => {
    if (item.kind !== "swarm_pending" || isSwarmPendingTerminal(item)) return item;

    const terminalFromResults = new Set(item.terminal_job_ids || []);
    for (const jid of item.job_ids) {
      if (resultByJob.has(jid)) terminalFromResults.add(jid);
    }

    // Objective cover for single-id local-swarm ↔ substrate id mismatch.
    let objectiveFailed = false;
    let objectiveCovered = false;
    if (item.job_ids.length === 1 && item.objective) {
      for (const res of resultByJob.values()) {
        if (res.objective !== item.objective) continue;
        objectiveCovered = true;
        if (res.failed) objectiveFailed = true;
      }
    }

    const allTerminal =
      item.job_ids.every((id) => terminalFromResults.has(id)) || objectiveCovered;
    if (allTerminal) {
      const anyFailed =
        objectiveFailed
        || [...terminalFromResults].some((id) => resultByJob.get(id)?.failed);
      return withPendingTerminal(
        item,
        anyFailed ? "failed" : "done",
        [...terminalFromResults],
      );
    }

    const anyLive = item.job_ids.some((id) => live.has(id));
    if (!anyLive) {
      return withPendingTerminal(item, "ended", [...(item.terminal_job_ids || [])]);
    }
    return item;
  });
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
