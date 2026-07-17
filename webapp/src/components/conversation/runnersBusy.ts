/**
 * Pure decisions for the detached-busy runners poll (session switch / Stop).
 */

import { turnHasLiveInvestigation } from "../../lib/turnProgress";
import type { Item } from "../TranscriptList";
import { shouldArmChatEventsFromRunners } from "./chatEvents";

export type BusyStatus = "idle" | "thinking" | "executing" | "done" | "error" | "streaming";

/** Force idle while userStopped sticks through runner unwind. */
export function userStoppedBusyChrome(status: BusyStatus): BusyStatus {
  if (status === "thinking" || status === "executing" || status === "streaming") {
    return "idle";
  }
  return status;
}

/** Preserve busy chrome when runners already report thinking/executing/streaming. */
export function preserveOrThinking(status: BusyStatus): BusyStatus {
  if (status === "thinking" || status === "executing" || status === "streaming") {
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
    if (turnHasLiveInvestigation(opts.items, true)) {
      return { kind: "hold_live_investigation" };
    }
    return { kind: "finalize_idle_refresh" };
  }

  return { kind: "noop" };
}
