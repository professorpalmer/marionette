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
  finalizeStreamingThinking,
  hoistCardsBeforeTrailingFinals,
  newThinkingId,
} from "./thinkingToolPrep";
import {
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
import {
  boundActionField,
  isTerminalJobStatus,
  MAX_ACTION_ERROR_CHARS,
  MAX_ACTION_GOAL_CHARS,
  MAX_ACTION_ID_CHARS,
  MAX_ACTION_KIND_CHARS,
  MAX_JOB_ACTIONS,
  normalizeNestedActionStatus,
} from "./nestedActionBounds";

export {
  boundActionField,
  isTerminalJobStatus,
  MAX_ACTION_ERROR_CHARS,
  MAX_ACTION_GOAL_CHARS,
  MAX_ACTION_ID_CHARS,
  MAX_ACTION_KIND_CHARS,
  MAX_JOB_ACTIONS,
  normalizeNestedActionStatus,
} from "./nestedActionBounds";

/**
 * Close every open pilot/reasoning surface before a new phase or turn terminal
 * (assistant_done, Stop, action_start prep). Later events may only APPEND.
 * Unlike finalizeOpenPilotBubble alone — which stops at tool-card fences so
 * later deltas open a post-card bubble — this clears streaming chrome even when
 * swarm pills or cards trail the bubble. Worker-stream previews are left alone
 * (ephemeral; action_result drops them).
 */
export function sealOpenStreamSurfaces(items: Item[]): Item[] {
  const withThinking = finalizeStreamingThinking(items);
  let changed = false;
  const next: Item[] = [];
  for (const it of withThinking) {
    if (
      it.kind === "msg"
      && it.msg.role === "assistant"
      && it.msg.streaming
      && !it.msg.workerStream
    ) {
      changed = true;
      const finalText = (it.msg.text || "").trim();
      if (!finalText) continue;
      next.push({ kind: "msg", msg: { ...it.msg, streaming: false } });
      continue;
    }
    next.push(it);
  }
  // Sealing can turn a short stream into a final-looking answer that already
  // has late Cursor tool cards after it — hoist so Explored stays above.
  return hoistCardsBeforeTrailingFinals(changed ? next : withThinking);
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

/**
 * Update-or-insert a durable investigation card from action_result.
 * Ring-miss / reload can deliver a result without a prior action_start; still
 * paint a card when kind/goal/status/duration/error are present.
 */
/** Keep failed / noisy run_command results expanded so exit + output stay visible. */
function shouldOpenActionResultCard(
  d: {
    kind?: string;
    error?: string;
    exit_code?: unknown;
    output?: unknown;
    [key: string]: unknown;
  },
  existingKind?: string,
): boolean {
  if (d.error) return true;
  const exitCode = d.exit_code;
  if (typeof exitCode === "number" && exitCode !== 0) return true;
  const kind = String(d.kind || existingKind || "").trim().toLowerCase();
  const isRun =
    kind === "run_command" || kind === "bash" || kind === "shell" || kind === "execute";
  const output = typeof d.output === "string" ? d.output.trim() : "";
  if (isRun && output && typeof exitCode === "number" && exitCode !== 0) return true;
  return false;
}

export function applyActionResultCard(
  items: Item[],
  d: {
    id?: string;
    kind?: string;
    goal?: string;
    goals?: string[];
    cwd?: string | null;
    call_id?: string;
    error?: string;
    duration_ms?: number;
    status?: string;
    message?: string;
    job_id?: string;
    num?: number;
    types?: string[];
    adapter?: string;
    artifacts?: { type: string; headline: string }[];
    chars?: number;
    auth_failure?: string;
    exit_code?: number;
    output?: string;
    command?: string;
    [key: string]: unknown;
  },
): Item[] {
  const id = String(d.id || "").trim();
  const callId = d.call_id ? String(d.call_id).trim() : "";
  if (!id && !callId) return items;
  const sealed = finalizeStreamingBubbleOnActionResult(items);
  const matchIdx = sealed.findIndex((it) => {
    if (it.kind !== "card") return false;
    const card = it.card;
    if (id && card.id === id) return true;
    if (callId && (card.call_id === callId || card.id === callId || card.id === `tool-prep:${callId}`)) {
      return true;
    }
    return false;
  });
  const outcome: TerminalJobOutcome = d.error
    || String(d.status || "").toLowerCase() === "failed"
    ? "failed"
    : String(d.status || "").toLowerCase() === "cancelled"
      || String(d.status || "").toLowerCase() === "canceled"
      ? "cancelled"
      : "complete";
  if (matchIdx >= 0) {
    const prev = sealed[matchIdx];
    if (prev.kind !== "card") return sealed;
    const settledActions = settleNestedRunning(prev.card.actions, outcome);
    const keepOpen = shouldOpenActionResultCard(d, prev.card.kind);
    return sealed.map((it, i) => {
      if (i !== matchIdx || it.kind !== "card") return it;
      return {
        kind: "card" as const,
        card: {
          ...it.card,
          running: false,
          open: keepOpen,
          result: d as Card["result"],
          ...(d.kind ? { kind: String(d.kind) } : {}),
          ...(d.goal != null && String(d.goal).trim() ? { goal: String(d.goal) } : {}),
          ...(Array.isArray(d.goals) ? { goals: d.goals.map(String) } : {}),
          ...(callId || it.card.call_id
            ? { call_id: callId || it.card.call_id }
            : {}),
          ...(settledActions !== it.card.actions ? { actions: settledActions } : {}),
        },
      };
    });
  }
  const kind = String(d.kind || "").trim();
  const goal = String(d.goal || "").trim();
  const hasBody =
    Boolean(kind || goal || d.error || d.duration_ms != null || d.status || d.message || d.job_id);
  if (!hasBody) return sealed;
  return [
    ...sealed,
    {
      kind: "card" as const,
      card: {
        id: id || callId,
        goal: goal || (Array.isArray(d.goals) ? d.goals.map(String).join(", ") : ""),
        cwd: d.cwd ?? null,
        kind: kind || undefined,
        call_id: callId || undefined,
        goals: Array.isArray(d.goals) ? d.goals.map(String) : undefined,
        running: false,
        open: shouldOpenActionResultCard(d, kind),
        result: d as Card["result"],
      },
    },
  ];
}

export type TerminalJobOutcome = "complete" | "failed" | "cancelled";

function cardMatchesJobId(card: Card, jobId: string): boolean {
  const raw = String(card.result?.job_id || "").trim();
  if (!raw || !jobId) return false;
  if (raw === jobId) return true;
  return raw.split(",").map((p) => p.trim()).filter(Boolean).includes(jobId);
}

function settleNestedRunning(
  actions: NonNullable<Card["actions"]> | undefined,
  outcome: TerminalJobOutcome,
): NonNullable<Card["actions"]> | undefined {
  if (!actions || actions.length === 0) return actions;
  const nextStatus: "complete" | "failed" =
    outcome === "complete" ? "complete" : "failed";
  const err =
    outcome === "cancelled"
      ? "cancelled"
      : outcome === "failed"
        ? "failed"
        : "";
  let changed = false;
  const next = actions.map((row) => {
    if (row.status !== "running") return row;
    changed = true;
    return {
      ...row,
      status: nextStatus,
      error: row.error || err,
    };
  });
  return changed ? next : actions;
}

/**
 * Idempotent scoped terminal reconciler: only cards whose result.job_id matches
 * (including comma-separated run_parallel child ids) clear card.running and
 * settle nested running rows. Unrelated live jobs/cards stay untouched.
 */
export function reconcileTerminalJobCards(
  items: Item[],
  jobId: string,
  outcome: TerminalJobOutcome = "complete",
): Item[] {
  const jid = String(jobId || "").trim();
  if (!jid) return items;
  let changed = false;
  const next = items.map((it) => {
    if (it.kind !== "card") return it;
    if (!cardMatchesJobId(it.card, jid)) return it;
    const settledActions = settleNestedRunning(it.card.actions, outcome);
    const actionsChanged = settledActions !== it.card.actions;
    if (!it.card.running && !actionsChanged) return it;
    changed = true;
    return {
      kind: "card" as const,
      card: {
        ...it.card,
        running: false,
        ...(actionsChanged ? { actions: settledActions } : {}),
      },
    };
  });
  return changed ? next : items;
}

export function terminalOutcomeFromSwarmResult(resObj: {
  applied?: boolean;
  error?: string | null;
}): TerminalJobOutcome {
  return swarmResultLooksFailed(resObj) ? "failed" : "complete";
}

export function terminalOutcomeFromJobStatus(
  status: string | undefined | null,
): TerminalJobOutcome {
  const s = String(status || "").trim().toLowerCase();
  if (s === "cancelled" || s === "canceled") return "cancelled";
  if (s === "failed" || s === "error") return "failed";
  return "complete";
}

/**
 * At assistant_done / turn boundary: settle orphan result:null and tool-prep
 * cards only when they are not owned by a still-live job id. Never globally
 * clear real background workers.
 *
 * Also clears stale ``running`` when a result body already landed (the common
 * "spinner forever on Read/Search" case) and settles nested rows once the
 * parent is no longer live.
 */
export function reconcileOrphanInvestigationCards(
  items: Item[],
  liveJobIds: ReadonlySet<string> | readonly string[],
): Item[] {
  const live = liveJobIds instanceof Set ? liveJobIds : new Set(liveJobIds);
  let changed = false;
  const next = items.map((it) => {
    if (it.kind !== "card") return it;
    const card = it.card;
    const jobId = String(card.result?.job_id || "").trim();
    const parts = jobId
      ? jobId.split(",").map((p) => p.trim()).filter(Boolean)
      : [];
    if (parts.some((p) => live.has(p))) return it;

    const nestedRunning = (card.actions || []).some((a) => a.status === "running");
    const hasResult = Boolean(card.result);
    const isPrep =
      typeof card.id === "string" && card.id.startsWith("tool-prep:");
    const orphanPending = card.running && !hasResult && !jobId;
    const resultStatus = String(card.result?.status || "").trim().toLowerCase();
    const resultTerminal =
      !jobId
      || resultStatus === "complete"
      || resultStatus === "completed"
      || resultStatus === "done"
      || resultStatus === "failed"
      || resultStatus === "error"
      || resultStatus === "cancelled"
      || resultStatus === "canceled"
      || resultStatus === "interrupted"
      || resultStatus === "stalled"
      || isTerminalJobStatus(card.result?.status);

    // Result already present (read/search/… or terminal job ack) but parent
    // still flagged running — clear the stale spinner without inventing errors.
    // Leave non-terminal job acks alone when the tracker momentarily omits them.
    if (card.running && hasResult && resultTerminal) {
      changed = true;
      return {
        kind: "card" as const,
        card: {
          ...card,
          running: false,
          actions: nestedRunning
            ? settleNestedRunning(card.actions, "complete")
            : card.actions,
        },
      };
    }

    // Nested-only stale after parent settled / never had a live job.
    if (!isPrep && !orphanPending) {
      if (nestedRunning && !jobId) {
        changed = true;
        return {
          kind: "card" as const,
          card: {
            ...card,
            actions: settleNestedRunning(card.actions, "complete"),
          },
        };
      }
      return it;
    }
    if (!card.running && !nestedRunning) return it;
    changed = true;
    return {
      kind: "card" as const,
      card: {
        ...card,
        running: false,
        actions: settleNestedRunning(card.actions, "complete"),
        result: card.result || { status: "interrupted", error: "missing action_result" },
      },
    };
  });
  return changed ? next : items;
}

/** True when a late swarmLive / runners-busy poll still belongs to the active session. */
export function shouldApplySwarmLiveMerge(opts: {
  pollGen: number;
  currentGen: number;
  pollSessionId: string | null;
  cachedSessionId: string | null;
  activeSessionId: string | null;
}): boolean {
  const sid = opts.pollSessionId;
  if (!sid) return false;
  if (opts.pollGen !== opts.currentGen) return false;
  if (opts.cachedSessionId !== sid) return false;
  if (opts.activeSessionId !== sid) return false;
  return true;
}

export type SwarmLiveReloadJob = {
  id?: string;
  status?: string;
  actions?: unknown;
};

/**
 * Fold swarmLive jobs into items after sessionTranscript reload.
 *
 * Empty local-job snapshots are not proof that non-job tool cards are orphaned —
 * mid-turn reload can race chatEvents reattach. Orphan settlement belongs only
 * at authoritative turn terminals (assistant_done / error / Stop). Returns the
 * prior items unchanged when there is nothing authoritative to merge.
 */
export function foldSwarmLiveJobsAfterReload(
  prev: Item[],
  jobs: readonly SwarmLiveReloadJob[],
): Item[] {
  const list = Array.isArray(jobs) ? jobs : [];
  const hasActions = list.some(
    (j) => Array.isArray(j.actions) && j.actions.length > 0,
  );
  const hasTerminal = list.some((j) => isTerminalJobStatus(j?.status));
  if (!hasActions && !hasTerminal) {
    return prev;
  }
  return mergeJobActionsIntoItems(prev, list as Array<{
    id?: string;
    actions?: unknown;
    status?: string;
  }>);
}

function nestedRowsEqual(
  a: NonNullable<Card["actions"]>,
  b: NonNullable<Card["actions"]>,
): boolean {
  if (a.length !== b.length) return false;
  return a.every((p, i) =>
    p.action_id === b[i].action_id
    && p.status === b[i].status
    && p.kind === b[i].kind
    && (p.goal || "") === (b[i].goal || "")
    && (p.error || "") === (b[i].error || "")
    && (p.duration_ms ?? null) === (b[i].duration_ms ?? null)
    && (p.worker_id || "") === (b[i].worker_id || "")
  );
}

function prevRowsForWorker(
  prev: NonNullable<Card["actions"]>,
  workerId: string,
): NonNullable<Card["actions"]> {
  return prev.filter((row) =>
    row.worker_id === workerId
    || row.action_id.startsWith(`${workerId}:`)
  );
}

/** Merge sanitized local-job actions[] onto investigation cards by job_id. */
export function mergeJobActionsIntoItems(
  items: Item[],
  jobs: Array<{ id?: string; actions?: unknown; status?: string }>,
): Item[] {
  if (!jobs.length) return items;
  const byJob = new Map<string, NonNullable<Card["actions"]>>();
  const statusByJob = new Map<string, string>();
  for (const job of jobs) {
    const jid = String(job.id || "").trim();
    if (!jid) continue;
    if (job.status != null && String(job.status).trim()) {
      statusByJob.set(jid, String(job.status).trim().toLowerCase());
    }
    if (!Array.isArray(job.actions)) continue;
    const rows: NonNullable<Card["actions"]> = [];
    for (const raw of job.actions) {
      if (!raw || typeof raw !== "object") continue;
      const r = raw as Record<string, unknown>;
      const actionId = String(r.action_id || "").trim();
      if (!actionId) continue;
      rows.push({
        action_id: boundActionField(actionId, MAX_ACTION_ID_CHARS),
        kind: boundActionField(r.kind || "tool_call", MAX_ACTION_KIND_CHARS) || "tool_call",
        goal: boundActionField(r.goal || "", MAX_ACTION_GOAL_CHARS),
        status: normalizeNestedActionStatus(r.status, r.error),
        duration_ms: typeof r.duration_ms === "number" ? r.duration_ms : null,
        error: r.error ? boundActionField(r.error, MAX_ACTION_ERROR_CHARS) : "",
        worker_id: jid,
      });
    }
    byJob.set(jid, rows);
  }
  if (byJob.size === 0 && statusByJob.size === 0) return items;
  let changed = false;
  let next = items.map((it) => {
    if (it.kind !== "card") return it;
    const jobId = String(it.card.result?.job_id || "").trim();
    if (!jobId) return it;
    const parts = jobId.split(",").map((p) => p.trim()).filter(Boolean);
    const prev = it.card.actions || [];
    let merged: NonNullable<Card["actions"]> = [];
    let sawSnapshot = false;

    if (parts.length <= 1) {
      const rows = byJob.get(jobId);
      if (rows) {
        sawSnapshot = true;
        merged = rows;
      } else if (!statusByJob.has(jobId)) {
        return it;
      } else {
        merged = prev;
      }
    } else {
      for (const part of parts) {
        const rows = byJob.get(part);
        if (rows) {
          sawSnapshot = true;
          for (const row of rows) {
            merged.push({
              ...row,
              action_id: row.action_id.startsWith(`${part}:`)
                ? row.action_id
                : `${part}:${row.action_id}`,
              worker_id: part,
            });
          }
        } else {
          // Partial live snapshot omitted this sibling — keep already-known rows.
          for (const row of prevRowsForWorker(prev, part)) {
            merged.push(row);
          }
        }
      }
      if (!sawSnapshot && !parts.some((p) => statusByJob.has(p))) {
        return it;
      }
    }

    if (merged.length > MAX_JOB_ACTIONS) {
      merged = merged.slice(-MAX_JOB_ACTIONS);
    }

    const terminalParts = parts.filter((p) => isTerminalJobStatus(statusByJob.get(p)));
    const allPartsTerminal =
      parts.length > 0 && parts.every((p) => isTerminalJobStatus(statusByJob.get(p)));
    // Single-id card: terminal when that job is terminal. Multi-id: only clear
    // parent running when every sibling is terminal; still settle nested rows
    // belonging to terminal parts.
    let actionsOut = merged;
    if (terminalParts.length > 0) {
      const outcomeFor = (workerId: string): TerminalJobOutcome =>
        terminalOutcomeFromJobStatus(statusByJob.get(workerId));
      actionsOut = merged.map((row) => {
        const wid = String(row.worker_id || "").trim();
        const owner =
          wid
          || parts.find((p) => row.action_id.startsWith(`${p}:`))
          || (parts.length === 1 ? parts[0] : "");
        if (!owner || !terminalParts.includes(owner)) return row;
        if (row.status !== "running") return row;
        const outcome = outcomeFor(owner);
        return {
          ...row,
          status: outcome === "complete" ? "complete" as const : "failed" as const,
          error: row.error || (
            outcome === "cancelled" ? "cancelled" : outcome === "failed" ? "failed" : ""
          ),
        };
      });
    }

    const clearRunning = allPartsTerminal
      || (parts.length <= 1 && isTerminalJobStatus(statusByJob.get(jobId)));
    const runningOut = clearRunning ? false : it.card.running;
    if (
      nestedRowsEqual(prev, actionsOut)
      && runningOut === it.card.running
      && (parts.length === 1 ? parts[0] : it.card.worker_id) === it.card.worker_id
    ) {
      return it;
    }
    changed = true;
    return {
      kind: "card" as const,
      card: {
        ...it.card,
        running: runningOut,
        actions: actionsOut,
        worker_id: parts.length === 1 ? parts[0] : it.card.worker_id,
      },
    };
  });

  // Status-only terminal jobs (no actions array in this snapshot) still need
  // scoped card.running / nested settle via the reconciler.
  for (const [jid, status] of statusByJob) {
    if (!isTerminalJobStatus(status)) continue;
    if (byJob.has(jid) && (byJob.get(jid) || []).length > 0) continue;
    const reconciled = reconcileTerminalJobCards(
      next,
      jid,
      terminalOutcomeFromJobStatus(status),
    );
    if (reconciled !== next) {
      changed = true;
      next = reconciled;
    }
  }
  return changed ? next : items;
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
      return hoistCardsBeforeTrailingFinals([
        ...p.slice(0, streamIdx),
        ...p.slice(streamIdx + 1),
      ]);
    }
    const updatedItems = [...p];
    updatedItems[streamIdx] = {
      kind: "msg",
      msg: { ...lastMsg.msg, text: finalText, streaming: false },
    };
    return hoistCardsBeforeTrailingFinals(
      deduplicateConsecutiveAssistantMessages(updatedItems),
    );
  }
  if (!text) return hoistCardsBeforeTrailingFinals(p);
  const incoming = text.trim();
  if (!incoming) return hoistCardsBeforeTrailingFinals(p);

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
    if (prior === incoming) return hoistCardsBeforeTrailingFinals(p);

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
    return hoistCardsBeforeTrailingFinals(
      deduplicateConsecutiveAssistantMessages(updated),
    );
  }

  return hoistCardsBeforeTrailingFinals(
    deduplicateConsecutiveAssistantMessages([
      ...p,
      {
        kind: "msg",
        msg: { role: "assistant", text: text || "", isPlan: opts?.isPlan },
      },
    ]),
  );
}

/** Idempotent action_start card append (session-switch SSE race safe). */
export function appendActionStartCard(
  items: Item[],
  d: {
    id: string;
    goal?: string;
    goals?: string[];
    cwd?: string | null;
    kind?: string;
    call_id?: string;
  },
): Item[] {
  // Seal thinking + pilot bubbles first so tool cards never absorb prior text.
  const sealed = sealOpenStreamSurfaces(items);
  if (sealed.some((it) => it.kind === "card" && it.card.id === d.id)) {
    return sealed.filter((it) => it.kind !== "tool_prep");
  }

  // Promote a provisional tool-prep hint IN PLACE when correlation is safe.
  // Never steal by kind-only or oldest-prep fallback — that mutated Read rows
  // into Write and collapsed Read→Write→Read into fewer cards.
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
  const callId = String(d.call_id || "").trim() || (
    // Provider-stable action ids are themselves call ids (not a{n} fallbacks).
    /^a\d+$/.test(String(d.id || "")) ? "" : String(d.id || "").trim()
  );
  const prepIdx =
    (callId
      ? prepIndexes.find((i) => {
          const card = (sealed[i] as Extract<Item, { kind: "card" }>).card;
          return card.id === `tool-prep:${callId}`;
        })
      : undefined)
    ?? prepIndexes.find((i) => {
      const card = (sealed[i] as Extract<Item, { kind: "card" }>).card;
      const cardGoal = String(card.goal || "").trim();
      const cardKind = String(card.kind || "").trim();
      return Boolean(goal) && Boolean(kind)
        && cardGoal === goal
        && cardKind === kind;
    });
  if (prepIdx != null) {
    return sealed
      .map((it, i) => (
        i === prepIdx
          ? {
              kind: "card" as const,
              card: {
                id: d.id,
                goal: (d.goal as string) || goal,
                goals: Array.isArray(d.goals) ? d.goals.map(String) : undefined,
                cwd: d.cwd,
                running: true,
                open: false,
                kind: d.kind,
                call_id: callId || undefined,
              },
            }
          : it
      ))
      .filter((it) => it.kind !== "tool_prep");
  }

  // No safe prep match: append a distinct durable card. Leave unrelated
  // provisional hints alone (clearToolPrepPlaceholders would wipe them).
  return [
    ...sealed.filter((it) => it.kind !== "tool_prep"),
    {
      kind: "card",
      card: {
        id: d.id,
        // Match prior Conversation wiring: goal may be undefined on the wire.
        goal: d.goal as string,
        goals: Array.isArray(d.goals) ? d.goals.map(String) : undefined,
        cwd: d.cwd,
        running: true,
        open: false,
        kind: d.kind,
        call_id: callId || undefined,
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
  // processed-job ref: terminal pill + existing result row → still reconcile
  // matching investigation cards (spinner settle) then no-op the pill/result.
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
    return reconcileTerminalJobCards(
      items,
      jobId,
      terminalOutcomeFromSwarmResult(resObj),
    );
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

  // Swarm pill terminalization alone leaves card.running / nested action.status
  // spinning — reconcile matching investigation cards even when polling stops.
  const outcome = terminalOutcomeFromSwarmResult(resObj);
  const reconciled = reconcileTerminalJobCards(updated, jobId, outcome);

  if (
    alreadyHasResult
    || reconciled.some((it) => it.kind === "swarm_result" && it.job_id === jobId)
  ) {
    return reconciled;
  }

  return [
    ...reconciled,
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
