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
import {
  findCanonicalSwarmPendingIndex,
  isSwarmPendingTerminal,
  mergeSwarmPendingReplay,
  normalizeSwarmJobIds,
  swarmPendingStatusOf,
} from "./swarmPendingIdentity";

/**
 * Seal every open stream surface (thinking row + pilot bubble) before a new
 * phase starts. Later events may only APPEND; they must not reopen, merge, or
 * re-parent content that already painted on a prior surface.
 */
export function sealOpenStreamSurfaces(items: Item[]): Item[] {
  return finalizeOpenPilotBubble(finalizeStreamingThinking(items));
}

export function swarmPendingStatus(item: SwarmPendingItem): SwarmPendingStatus {
  return swarmPendingStatusOf(item);
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

/** User-visible notice kinds that paint in the composer wait-hint chrome. */
export function noticeShowsWaitHint(kind?: string | null): boolean {
  return !kind || kind === "wait" || kind === "stagnation" || kind === "resume_cap";
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
  opts?: { isPlan?: boolean; streamed?: boolean },
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
  const incoming = text.trim();
  if (!incoming) return p;

  // Idempotent finals: exact identity always no-ops. Streamed finals may only
  // extend a sealed bubble when nothing (card/tool/thinking/msg) follows it —
  // never rewrite a pre-tool bubble above a later card (that reorders narration).
  for (let j = p.length - 1; j >= 0; j--) {
    const it = p[j];
    if (it.kind === "msg" && it.msg.role === "user") break;
    if (it.kind !== "msg" || it.msg.role !== "assistant") continue;
    if (it.msg.streaming || it.msg.workerStream) continue;
    const prior = (it.msg.text || "").trim();
    if (!prior) continue;
    if (prior === incoming) return p;

    if (!opts?.streamed) continue;
    if (!(incoming.startsWith(prior) && incoming.length > prior.length)) continue;
    const hasLaterSurface = p.slice(j + 1).some((later) => (
      later.kind === "card"
      || later.kind === "tool_prep"
      || later.kind === "thinking"
      || (
        later.kind === "msg"
        && later.msg.role === "assistant"
      )
    ));
    // Card (or any later surface) after this bubble → append a new answer.
    if (hasLaterSurface) continue;
    const updated = [...p];
    updated[j] = {
      kind: "msg",
      msg: {
        ...it.msg,
        text,
        streaming: false,
        isPlan: opts?.isPlan ?? it.msg.isPlan,
      },
    };
    return deduplicateConsecutiveAssistantMessages(updated);
  }

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
  const sealed = sealOpenStreamSurfaces(items);
  if (sealed.some((it) => it.kind === "card" && it.card.id === d.id)) {
    return sealed.filter((it) => it.kind !== "tool_prep");
  }

  // Promote the eager provider tool hint into the real action row IN PLACE.
  // Clearing and re-appending the provisional card briefly removed the only
  // visible execution surface between React updates ("Still working…" with no
  // terminal row). Prefer exact goal+kind, then kind, then the oldest prep.
  const prepIndexes: number[] = [];
  for (let i = 0; i < sealed.length; i++) {
    const it = sealed[i];
    if (
      it.kind === "card"
      && typeof it.card.id === "string"
      && it.card.id.startsWith("tool-prep:")
    ) {
      prepIndexes.push(i);
    }
  }
  const goal = String(d.goal || "").trim();
  const kind = String(d.kind || "").trim();
  const prepIdx =
    prepIndexes.find((i) => {
      const card = (sealed[i] as Extract<Item, { kind: "card" }>).card;
      return goal && card.goal === goal && (!kind || card.kind === kind);
    })
    ?? prepIndexes.find((i) => {
      const card = (sealed[i] as Extract<Item, { kind: "card" }>).card;
      return kind && card.kind === kind;
    })
    ?? prepIndexes[0];
  if (prepIdx != null) {
    return sealed
      .map((it, i) => (
        i === prepIdx
          ? {
              kind: "card" as const,
              card: {
                id: d.id,
                goal: d.goal as string,
                cwd: d.cwd,
                running: true,
                open: false,
                kind: d.kind,
              },
            }
          : it
      ))
      .filter((it) => it.kind !== "tool_prep");
  }

  const base = clearToolPrepPlaceholders(sealed);
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

/**
 * Upsert a swarm lifecycle pill by canonical job-id identity. Replay / SSE
 * echoes update the existing row in place and never resurrect running over a
 * terminal status (done / failed / ended).
 */
export function appendSwarmPending(
  items: Item[],
  jobIds: string[],
  objective: string,
): Item[] {
  const normalizedIds = normalizeSwarmJobIds(jobIds);
  const obj = objective || "";
  if (normalizedIds.length === 0 && !obj.trim()) return items;

  const existingIdx = findCanonicalSwarmPendingIndex(
    items,
    normalizedIds,
    obj,
    // Pending replay must not collapse distinct jobs that merely share a goal.
    { allowObjectiveAlias: false },
  );
  if (existingIdx >= 0 && items[existingIdx].kind === "swarm_pending") {
    const existing = items[existingIdx] as SwarmPendingItem;
    const merged = mergeSwarmPendingReplay(existing, normalizedIds, obj);
    if (
      merged.status === existing.status
      && merged.resolved === existing.resolved
      && merged.objective === existing.objective
      && merged.job_ids.length === existing.job_ids.length
      && merged.job_ids.every((id, i) => id === existing.job_ids[i])
      && (merged.terminal_job_ids || []).length === (existing.terminal_job_ids || []).length
      && (merged.terminal_job_ids || []).every(
        (id, i) => id === (existing.terminal_job_ids || [])[i],
      )
    ) {
      return items;
    }
    const updated = items.slice();
    updated[existingIdx] = merged;
    return updated;
  }

  return [
    ...items,
    {
      kind: "swarm_pending" as const,
      job_ids: normalizedIds,
      objective: obj,
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
function findSwarmPendingForResult(
  items: Item[],
  jobId: string,
  objective?: string,
): number {
  // Prefer the most advanced row that already lists this job id (handles
  // historical duplicate pills before display dedupe collapses them).
  let bestIdx = -1;
  let bestRank = -1;
  for (let i = 0; i < items.length; i++) {
    const it = items[i];
    if (it.kind !== "swarm_pending" || !it.job_ids.includes(jobId)) continue;
    const rank = isSwarmPendingTerminal(it)
      ? swarmPendingStatus(it) === "failed"
        ? 3
        : swarmPendingStatus(it) === "done"
          ? 2
          : 1
      : 0;
    if (bestIdx < 0 || rank > bestRank) {
      bestIdx = i;
      bestRank = rank;
    }
  }
  if (bestIdx >= 0) return bestIdx;

  // Sync run_swarm emits pending as local-swarm-{aid} but swarm_result may
  // carry the substrate job id — conservative single-id objective alias only
  // when at least one side is a local-swarm placeholder (never collapse two
  // distinct substrate jobs that share a goal).
  if (!objective) return -1;
  let aliasIdx = -1;
  let aliasRank = -1;
  for (let i = 0; i < items.length; i++) {
    const it = items[i];
    if (it.kind !== "swarm_pending") continue;
    if (it.job_ids.length !== 1) continue;
    if (it.objective !== objective) continue;
    const existingId = it.job_ids[0];
    if (
      existingId !== jobId
      && !existingId.startsWith("local-swarm-")
      && !jobId.startsWith("local-swarm-")
    ) {
      continue;
    }
    const rank = isSwarmPendingTerminal(it)
      ? swarmPendingStatus(it) === "failed"
        ? 3
        : swarmPendingStatus(it) === "done"
          ? 2
          : 1
      : 0;
    // Prefer still-running so a spinner clears; otherwise keep most advanced.
    const prefer = !isSwarmPendingTerminal(it) ? 10 : rank;
    if (aliasIdx < 0 || prefer > aliasRank) {
      aliasIdx = i;
      aliasRank = prefer;
    }
  }
  return aliasIdx;
}

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
  const jobId = String(d.job_id || "").trim();
  if (!jobId) return items;

  const resObj = d.result || d;
  const resultFailed = swarmResultLooksFailed(resObj);
  const alreadyHasResult = items.some(
    (it) => it.kind === "swarm_result" && it.job_id === jobId,
  );

  const pendingIdx = findSwarmPendingForResult(items, jobId, d.objective);

  const pendingItem =
    pendingIdx >= 0 && items[pendingIdx].kind === "swarm_pending"
      ? (items[pendingIdx] as SwarmPendingItem)
      : null;
  const finalObjective = d.objective || pendingItem?.objective || "";

  // Idempotent across poll/SSE/rehydrate even when session-switch clears the
  // processed-job ref: terminal pill + existing result row → no-op.
  if (
    alreadyHasResult
    && pendingItem
    && isSwarmPendingTerminal(pendingItem)
    && (
      (pendingItem.terminal_job_ids || []).includes(jobId)
      || pendingItem.job_ids.includes(jobId)
      || (
        pendingItem.job_ids.length === 1
        && Boolean(d.objective)
        && pendingItem.objective === d.objective
      )
    )
  ) {
    return items;
  }

  const updated = items.map((item, idx) => {
    if (idx !== pendingIdx || item.kind !== "swarm_pending") return item;

    const creditedIds = item.job_ids.includes(jobId)
      ? [jobId]
      : item.job_ids.length === 1
        ? item.job_ids
        : [jobId];
    const terminalJobIds = normalizeSwarmJobIds([
      ...(item.terminal_job_ids || []),
      ...creditedIds,
    ]);
    const allTerminal = item.job_ids.every((id) => terminalJobIds.includes(id));
    if (!allTerminal) {
      if (isSwarmPendingTerminal(item)) {
        return { ...item, terminal_job_ids: terminalJobIds };
      }
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

  if (
    alreadyHasResult
    || updated.some((it) => it.kind === "swarm_result" && it.job_id === jobId)
  ) {
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
