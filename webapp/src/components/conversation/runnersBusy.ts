/**
 * Pure decisions for the detached-busy runners poll (session switch / Stop).
 */

import { turnHasVisibleBusySurface } from "../../lib/turnProgress";
import type { Item } from "../TranscriptList";
import { shouldArmChatEventsFromRunners } from "./chatEvents";

export type BusyStatus = "idle" | "thinking" | "executing" | "done" | "error" | "streaming" | "awaiting_swarm";

/**
 * Sticky agent-loop latch shared by Conversation + TranscriptList.
 * Includes awaiting_swarm so Investigating / absorption stay armed while workers fly.
 * This is NOT the composer mouth — see isPilotMouthBusy.
 */
export function isAgentLoopOpen(
  turnOpen: boolean,
  status: BusyStatus | string,
): boolean {
  return (
    turnOpen
    || status === "thinking"
    || status === "executing"
    || status === "streaming"
    || status === "awaiting_swarm"
  );
}

/**
 * Composer mouth: Stop/Steer only while the pilot's short turn is open.
 * A flying PM job (awaiting_swarm) is running, not busy — Send stays Send.
 */
export function isPilotMouthBusy(
  turnOpen: boolean,
  status: BusyStatus | string,
): boolean {
  return (
    turnOpen
    || status === "thinking"
    || status === "executing"
    || status === "streaming"
  );
}

/** Idle polls required before clearing detached busy (resist one false idle). */
export const RUNNERS_IDLE_CONFIRM_POLLS = 2;

/** Force idle while userStopped sticks through runner unwind. */
export function userStoppedBusyChrome(status: BusyStatus): BusyStatus {
  if (
    status === "thinking"
    || status === "executing"
    || status === "streaming"
    || status === "awaiting_swarm"
  ) {
    return "idle";
  }
  return status;
}

/** Preserve busy chrome when runners already report thinking/executing/streaming. */
export function preserveOrThinking(status: BusyStatus): BusyStatus {
  if (
    status === "thinking"
    || status === "executing"
    || status === "streaming"
    || status === "awaiting_swarm"
  ) {
    return status;
  }
  return "thinking";
}

export type RunnersBusyTickDecision =
  | { kind: "force_idle" }
  | { kind: "arm_reattach" }
  | { kind: "skip_disk_while_reattach" }
  | { kind: "refresh_busy_transcript" }
  | { kind: "hold_live_investigation" }
  | { kind: "hold_idle_unconfirmed" }
  | { kind: "finalize_idle_refresh" }
  | { kind: "noop" };

/**
 * One tick of the runners poll after getSessionState resolves.
 * Mirrors Conversation's detached-busy branch ordering.
 */
export function runnersBusyTickDecision(opts: {
  userStopped: boolean;
  localStreamActive: boolean;
  runnerBusy: boolean;
  detachedBusy: boolean;
  chatEventsPollArmed: boolean;
  items: Item[];
  /** Consecutive idle polls while detachedBusy (1 = first idle sighting). */
  consecutiveIdlePolls?: number;
  /** Idle polls required before finalize (default RUNNERS_IDLE_CONFIRM_POLLS). */
  idleConfirmPolls?: number;
}): RunnersBusyTickDecision {
  if (opts.userStopped) return { kind: "force_idle" };
  if (opts.localStreamActive) return { kind: "noop" };

  if (opts.runnerBusy) {
    if (
      shouldArmChatEventsFromRunners({
        runnerBusy: true,
        localStreamActive: opts.localStreamActive,
        userStopped: opts.userStopped,
        chatEventsPollArmed: opts.chatEventsPollArmed,
      })
    ) {
      return { kind: "arm_reattach" };
    }
    if (opts.chatEventsPollArmed) return { kind: "skip_disk_while_reattach" };
    return { kind: "refresh_busy_transcript" };
  }

  if (opts.detachedBusy) {
    // Runner already idle: only hold while a surface is actually live
    // (running card / tool_prep / streaming). Completed cards must NOT keep
    // Stop forever — sticky-between-batches applies while runnerBusy is true.
    if (turnHasVisibleBusySurface(opts.items)) {
      return { kind: "hold_live_investigation" };
    }
    // Resist a single transient false idle poll before clearing Stop.
    const needed = opts.idleConfirmPolls ?? RUNNERS_IDLE_CONFIRM_POLLS;
    const idlePolls = opts.consecutiveIdlePolls ?? 0;
    if (idlePolls < needed) {
      return { kind: "hold_idle_unconfirmed" };
    }
    return { kind: "finalize_idle_refresh" };
  }

  return { kind: "noop" };
}

/**
 * A live EventSource can stay "active" after the backend runner is already
 * idle (missed interrupted/assistant_done, zombie SSE). Runner polling used
 * to skip entirely while localStreamActive, so Investigating never cleared
 * without a restart. Confirm idle, then abandon that stream.
 */
export type StaleLocalStreamDecision =
  | { kind: "noop" }
  | { kind: "hold_unconfirmed" }
  | { kind: "abandon" };

export function staleLocalStreamTickDecision(opts: {
  localStreamActive: boolean;
  userStopped: boolean;
  runnerBusy: boolean;
  awaitingSwarm: boolean;
  turnSettled: boolean;
  /** True after this EventSource has seen runners=running or awaiting_swarm. */
  sawRunnerBusyThisStream: boolean;
  consecutiveIdlePolls: number;
  idleConfirmPolls?: number;
}): StaleLocalStreamDecision {
  if (!opts.localStreamActive || opts.userStopped) return { kind: "noop" };
  if (opts.turnSettled) return { kind: "noop" };
  if (opts.runnerBusy || opts.awaitingSwarm) return { kind: "noop" };
  // Do not abandon a just-opened stream before the runner latch appears.
  if (!opts.sawRunnerBusyThisStream) return { kind: "noop" };
  const needed = opts.idleConfirmPolls ?? RUNNERS_IDLE_CONFIRM_POLLS;
  if (opts.consecutiveIdlePolls < needed) return { kind: "hold_unconfirmed" };
  return { kind: "abandon" };
}
