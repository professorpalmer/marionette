/**
 * Pure helpers for the swarm-results poll tick (pending jobs while idle).
 *
 * Background dispatch ends the model turn on purpose (pause point), then the
 * UI holds Cursor-style "Still working…" chrome until the job lands and
 * keep-alive resume starts the next model turn.
 */

import type { Item } from "../TranscriptList";
import { isSwarmPendingTerminal } from "./swarmPendingIdentity";
import { formatDistilledNotice, formatWikiAutoIngestNotice } from "./streamApply";

/** Composer / pill hint while a real background job is still in flight. */
export const SWARM_AWAIT_HINT = "Still working…";

/** Keep-alive hint painted when pilot_resume kicks the next model turn. */
export const PILOT_LOOKING_HINT = "Looking…";

/**
 * True for durable background job ids (local-* UUID / job_* / etc.).
 * Placeholder local-swarm-* pills finish inside the turn and do not hold await chrome.
 */
export function hasLiveBackgroundJobIds(jobIds: readonly string[]): boolean {
  for (const raw of jobIds) {
    const id = String(raw || "").trim();
    if (!id) continue;
    if (id.startsWith("local-swarm-")) continue;
    return true;
  }
  return false;
}

/** Hold Stop/Steer + busy chrome after assistant_done while workers fly. */
export function shouldHoldSwarmAwaitChrome(opts: {
  pendingJobIds: readonly string[];
  backendPendingSwarms: boolean;
  userStopped: boolean;
}): boolean {
  if (opts.userStopped) return false;
  if (opts.backendPendingSwarms) return true;
  return hasLiveBackgroundJobIds(opts.pendingJobIds);
}

/**
 * True when getSessionState reports a background pause-point (await chrome).
 * Stop must suppress restore so we never re-paint Still working… after abandon.
 */
export function sessionStateShowsAwaitingSwarm(opts: {
  state?: string | null;
  pendingSwarms?: boolean;
  userStopped?: boolean;
}): boolean {
  if (opts.userStopped) return false;
  return opts.state === "awaiting_swarm" || !!opts.pendingSwarms;
}

/**
 * Rehydrate local pendingJobIds after session-switch transcript hydrate so
 * shouldHoldSwarmAwaitChrome can hold from local ids (not only backend peek).
 * Excludes local-swarm-* placeholders and terminal swarm_pending rows.
 */
export function seedPendingJobIdsFromHydrate(opts: {
  items: readonly Item[];
  transcriptJobIds?: readonly string[] | null;
}): string[] {
  const ids: string[] = [];
  const seen = new Set<string>();
  const push = (raw: unknown) => {
    const id = String(raw || "").trim();
    if (!id || id.startsWith("local-swarm-") || seen.has(id)) return;
    seen.add(id);
    ids.push(id);
  };
  for (const it of opts.items) {
    if (it.kind !== "swarm_pending") continue;
    if (isSwarmPendingTerminal(it)) continue;
    const terminals = new Set(it.terminal_job_ids || []);
    for (const jobId of it.job_ids || []) {
      if (terminals.has(jobId)) continue;
      push(jobId);
    }
  }
  for (const jobId of opts.transcriptJobIds || []) {
    push(jobId);
  }
  return ids;
}

/** Wait hint to paint (or clear) when the turn closes after dispatch. */
export function waitHintForAssistantDone(liveJobIds: readonly string[]): string | null {
  return hasLiveBackgroundJobIds(liveJobIds) ? SWARM_AWAIT_HINT : null;
}

/** True for Still working… / Looking… await chrome. */
export function isSwarmAwaitWaitHint(hint: string | null | undefined): boolean {
  return hint === SWARM_AWAIT_HINT || hint === PILOT_LOOKING_HINT;
}

/** Drop await wait hints; leave unrelated composer footnotes alone. */
export function clearSwarmAwaitWaitHint(prev: string | null): string | null {
  return isSwarmAwaitWaitHint(prev) ? null : prev;
}

/**
 * Drop job ids that swarm/live already reports as terminal.
 * pendingJobIds are normally cleared in handleSwarmResult; when drain misses
 * swarm_result, live terminal status must still release await chrome.
 */
export function pruneTerminalJobIds(
  pending: readonly string[],
  terminalIds: readonly string[],
): string[] {
  if (!pending.length) return [];
  if (!terminalIds.length) return pending.slice();
  const terminal = new Set<string>();
  for (const raw of terminalIds) {
    const id = String(raw || "").trim();
    if (id) terminal.add(id);
  }
  if (!terminal.size) return pending.slice();
  return pending.filter((id) => !terminal.has(id));
}

/** Job ids from swarm/live rows whose status is already terminal. */
export function terminalJobIdsFromSwarmLive(
  jobs: readonly { job_id?: string; id?: string; status?: string }[],
): string[] {
  const out: string[] = [];
  for (const job of jobs) {
    const status = String(job?.status || "").toLowerCase();
    if (
      status !== "complete"
      && status !== "completed"
      && status !== "failed"
      && status !== "cancelled"
      && status !== "canceled"
      && status !== "done"
    ) {
      continue;
    }
    const id = String(job?.job_id || job?.id || "").trim();
    if (id) out.push(id);
  }
  return out;
}

/**
 * Trailing getSessionState apply on the swarm-results poll: whether to clear
 * awaiting_swarm / Looking… / Still working… after jobs drain or Stop.
 * cancelArmed only gates resume queueing (triggerResumeGate); drained jobs
 * must clear await chrome even while a stream cancel token is armed.
 */
export function swarmResultsAwaitChromeClear(opts: {
  pendingSwarms: boolean;
  localPendingJobCount: number;
  userStopped: boolean;
  cancelArmed: boolean;
}): { clearAwaitStatus: boolean; clearWaitHint: boolean } {
  // cancelArmed is reserved for triggerResumeGate; ignore here so drained
  // jobs cannot leave awaiting_swarm stuck while an armed resume stream owns
  // thinking chrome via executeSend.
  void opts.cancelArmed;
  // Stop suppressed keep-alive — never leave Looking… painted.
  if (opts.userStopped) {
    return { clearAwaitStatus: true, clearWaitHint: true };
  }
  if (!opts.pendingSwarms && opts.localPendingJobCount === 0) {
    return { clearAwaitStatus: true, clearWaitHint: true };
  }
  return { clearAwaitStatus: false, clearWaitHint: false };
}

/**
 * Idle-session pilot_resume from swarm-results poll.
 * Stop must suppress the kick and clear Looking… rather than paint it.
 */
export function pilotResumePollAction(opts: {
  userStopped: boolean;
  alreadyFired: boolean;
}): "suppress_clear_hint" | "fire_looking" | "queue" {
  if (opts.userStopped) return "suppress_clear_hint";
  if (opts.alreadyFired) return "queue";
  return "fire_looking";
}

/**
 * triggerResume entry: Stop clears hints; cancelRef (stream armed) queues
 * keep-alive and must also clear Looking… / Still working… so poll-path
 * pilot_resume cannot leave a stuck hint while executeSend is deferred.
 */
export function triggerResumeGate(opts: {
  userStopped: boolean;
  cancelArmed: boolean;
}): "suppress_clear_hint" | "queue_clear_hint" | "execute" {
  if (opts.userStopped) return "suppress_clear_hint";
  if (opts.cancelArmed) return "queue_clear_hint";
  return "execute";
}

export type SwarmPollChrome =
  | { kind: "swarm_result"; data: any }
  | { kind: "pending_review"; data: { id?: string; summary?: string } }
  | { kind: "pilot_resume" }
  | { kind: "distilled"; notice: string }
  | { kind: "wiki_auto"; notice: string }
  | { kind: "wiki_prepare"; pages: any[] }
  | { kind: "memory_propose"; id: string; text: string; category: string; refine?: { kind: string; scope: string } }
  | { kind: "ignore" };

/** Classify one swarm poll event into a chrome action (no side effects). */
export function classifySwarmPollEvent(evt: any): SwarmPollChrome {
  const anyEvt = evt as any;
  if (anyEvt.kind === "swarm_result" && anyEvt.data) {
    return { kind: "swarm_result", data: anyEvt.data };
  }
  if (anyEvt.kind === "pending_review" && anyEvt.data) {
    return { kind: "pending_review", data: anyEvt.data };
  }
  if (anyEvt.kind === "pilot_resume") {
    return { kind: "pilot_resume" };
  }
  if (anyEvt.kind === "distilled" && anyEvt.data) {
    const notice = formatDistilledNotice(anyEvt.data);
    if (notice) return { kind: "distilled", notice };
    return { kind: "ignore" };
  }
  if (anyEvt.kind === "wiki_prepared" && anyEvt.data) {
    const d = anyEvt.data;
    const pages = d.pages || [];
    if (pages.length === 0) return { kind: "ignore" };
    if (d.auto_ingested) {
      const notice = formatWikiAutoIngestNotice(pages.length);
      return { kind: "wiki_auto", notice };
    }
    return { kind: "wiki_prepare", pages };
  }
  if (
    (anyEvt.kind === "memory_propose" || anyEvt.kind === "refine_propose")
    && anyEvt.data
  ) {
    const d = anyEvt.data;
    const id = d.id || "";
    const text = (d.text || "").trim();
    if (id && text) {
      return {
        kind: "memory_propose",
        id,
        text,
        category: d.category || "general",
        refine:
          anyEvt.kind === "refine_propose"
            ? {
                kind: String(d.kind || "memory"),
                scope: String(d.scope || "global"),
              }
            : undefined,
      };
    }
  }
  return { kind: "ignore" };
}

/** Deduplicate memory proposals by id. */
export function appendMemoryProposal<T extends { id: string }>(
  prev: T[],
  proposal: T,
): T[] {
  return prev.some((p) => p.id === proposal.id) ? prev : [...prev, proposal];
}
