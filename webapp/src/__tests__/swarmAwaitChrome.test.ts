import { describe, expect, it } from "vitest";
import {
  clearSwarmAwaitWaitHint,
  hasLiveBackgroundJobIds,
  PILOT_LOOKING_HINT,
  pilotResumePollAction,
  seedPendingJobIdsFromHydrate,
  sessionStateShowsAwaitingSwarm,
  shouldHoldSwarmAwaitChrome,
  SWARM_AWAIT_HINT,
  swarmResultsAwaitChromeClear,
  triggerResumeGate,
  waitHintForAssistantDone,
} from "../components/conversation/swarmPoll";
import { runnerBusySwitchDecision } from "../components/conversation/sessionHydrate";
import { shouldApplySwarmLiveMerge } from "../components/conversation/streamApply";
import { deriveBusyProgress, shouldShowBusyFooter } from "../lib/turnProgress";
import { derivePillStatus } from "../components/conversation/pillStatus";
import { isAgentLoopOpen } from "../components/conversation/runnersBusy";
import { statusPillClickable, statusPillLabel } from "../components/conversation/StatusPill";
import type { Item } from "../components/TranscriptList";

function msg(role: "user" | "assistant", text: string): Item {
  return { kind: "msg", msg: { role, text } };
}

describe("swarm await chrome", () => {
  it("treats local-* and job_* as live background ids", () => {
    expect(hasLiveBackgroundJobIds(["local-swarm-a"])).toBe(false);
    expect(hasLiveBackgroundJobIds(["local-bf1b30f4"])).toBe(true);
    expect(hasLiveBackgroundJobIds(["job_abcdef012345"])).toBe(true);
    expect(hasLiveBackgroundJobIds(["local-swarm-a", "local-x"])).toBe(true);
  });

  it("holds chrome while jobs fly and clears on Stop", () => {
    expect(
      shouldHoldSwarmAwaitChrome({
        pendingJobIds: ["local-bf1b30f4"],
        backendPendingSwarms: false,
        userStopped: false,
      }),
    ).toBe(true);
    expect(
      shouldHoldSwarmAwaitChrome({
        pendingJobIds: ["local-bf1b30f4"],
        backendPendingSwarms: false,
        userStopped: true,
      }),
    ).toBe(false);
    expect(
      shouldHoldSwarmAwaitChrome({
        pendingJobIds: [],
        backendPendingSwarms: true,
        userStopped: false,
      }),
    ).toBe(true);
  });

  it("busy latch ORs shouldHoldSwarmAwaitChrome with agentLoopOpen", () => {
    const hold = shouldHoldSwarmAwaitChrome({
      pendingJobIds: ["job_alive"],
      backendPendingSwarms: false,
      userStopped: false,
    });
    expect(hold).toBe(true);
    // Idle status after switch flap still keeps Stop/Steer via the hold.
    expect(isAgentLoopOpen(false, "idle") || hold).toBe(true);
    expect(
      isAgentLoopOpen(false, "idle")
        || shouldHoldSwarmAwaitChrome({
          pendingJobIds: ["job_alive"],
          backendPendingSwarms: false,
          userStopped: true,
        }),
    ).toBe(false);
  });

  it("sessionStateShowsAwaitingSwarm restores chrome unless Stop stuck", () => {
    expect(
      sessionStateShowsAwaitingSwarm({
        state: "awaiting_swarm",
        pendingSwarms: false,
        userStopped: false,
      }),
    ).toBe(true);
    expect(
      sessionStateShowsAwaitingSwarm({
        state: "idle",
        pendingSwarms: true,
        userStopped: false,
      }),
    ).toBe(true);
    expect(
      sessionStateShowsAwaitingSwarm({
        state: "awaiting_swarm",
        pendingSwarms: true,
        userStopped: true,
      }),
    ).toBe(false);
    expect(
      sessionStateShowsAwaitingSwarm({
        state: "idle",
        pendingSwarms: false,
        userStopped: false,
      }),
    ).toBe(false);
  });

  it("runnerBusySwitchDecision prefers awaiting over thinking when pending", () => {
    expect(
      runnerBusySwitchDecision({
        runnerState: "running",
        localStreamActive: false,
        switchedSession: true,
        pendingSwarms: true,
        sessionState: "idle",
      }).kind,
    ).toBe("awaiting");
    expect(
      runnerBusySwitchDecision({
        runnerState: "idle",
        localStreamActive: false,
        switchedSession: true,
        sessionState: "awaiting_swarm",
      }).kind,
    ).toBe("awaiting");
    expect(
      runnerBusySwitchDecision({
        runnerState: "running",
        localStreamActive: false,
        switchedSession: true,
        pendingSwarms: false,
        sessionState: "thinking",
      }).kind,
    ).toBe("busy");
  });

  it("seeds pendingJobIds from hydrate swarm_pending / job_ids (skips placeholders)", () => {
    const items: Item[] = [
      {
        kind: "swarm_pending",
        job_ids: ["local-swarm-a", "local-bf1b30f4"],
        objective: "fix",
        status: "running",
        resolved: false,
        terminal_job_ids: [],
      },
      {
        kind: "swarm_pending",
        job_ids: ["job_done_already"],
        objective: "done",
        status: "done",
        resolved: true,
        terminal_job_ids: ["job_done_already"],
      },
    ];
    expect(
      seedPendingJobIdsFromHydrate({
        items,
        transcriptJobIds: ["job_from_transcript", "local-swarm-skip"],
      }),
    ).toEqual(["local-bf1b30f4", "job_from_transcript"]);
    expect(
      seedPendingJobIdsFromHydrate({
        items: [msg("user", "hi")],
        transcriptJobIds: [],
      }),
    ).toEqual([]);
  });

  it("paints Still working hint after assistant_done with live jobs", () => {
    expect(waitHintForAssistantDone(["local-bf1b30f4"])).toBe(SWARM_AWAIT_HINT);
    expect(waitHintForAssistantDone(["local-swarm-a"])).toBe(null);
    expect(waitHintForAssistantDone([])).toBe(null);
  });

  it("keeps busy footer after end-turn summary while awaiting_swarm", () => {
    const items: Item[] = [
      msg("user", "fix layout"),
      msg("assistant", "Dispatched worker. Validating when it lands."),
    ];
    expect(shouldShowBusyFooter(items, "awaiting_swarm")).toBe(true);
    const p = deriveBusyProgress(items, "awaiting_swarm", 12_000, {
      waitHint: SWARM_AWAIT_HINT,
    });
    expect(p.phase).toBe("waiting");
    expect(p.label).toContain("Still working");
    expect(p.label).toContain("12s");
    // Must not early-idle just because the summary looks complete.
    expect(p.pill).not.toBe("idle");
  });

  it("pill prefers awaiting_swarm over answer-complete idle", () => {
    expect(
      derivePillStatus({
        transcriptStale: false,
        answerChromeIdle: true,
        liveInvestigation: false,
        turnOpen: false,
        status: "awaiting_swarm",
        awaitingSwarm: true,
      }),
    ).toBe("awaiting_swarm");
  });

  it("agentLoopOpen latch and StatusPill stay clickable Still working… while awaiting", () => {
    expect(isAgentLoopOpen(false, "awaiting_swarm")).toBe(true);
    expect(statusPillLabel("awaiting_swarm")).toBe("Still working…");
    expect(statusPillClickable("awaiting_swarm", undefined, () => {})).toBe(true);
  });

  it("clears Looking… / Still working… when Stop suppresses pilot_resume", () => {
    expect(pilotResumePollAction({ userStopped: true, alreadyFired: false })).toBe(
      "suppress_clear_hint",
    );
    expect(pilotResumePollAction({ userStopped: false, alreadyFired: false })).toBe(
      "fire_looking",
    );
    expect(pilotResumePollAction({ userStopped: false, alreadyFired: true })).toBe("queue");
    expect(clearSwarmAwaitWaitHint(PILOT_LOOKING_HINT)).toBeNull();
    expect(clearSwarmAwaitWaitHint(SWARM_AWAIT_HINT)).toBeNull();
    expect(clearSwarmAwaitWaitHint("Compacting…")).toBe("Compacting…");
  });

  it("triggerResume cancelRef queue clears await hints like Stop", () => {
    expect(
      triggerResumeGate({ userStopped: true, cancelArmed: false }),
    ).toBe("suppress_clear_hint");
    expect(
      triggerResumeGate({ userStopped: false, cancelArmed: true }),
    ).toBe("queue_clear_hint");
    expect(
      triggerResumeGate({ userStopped: false, cancelArmed: false }),
    ).toBe("execute");
    // queue_clear_hint must drop Looking… / Still working… (not leave stuck chrome).
    expect(clearSwarmAwaitWaitHint(PILOT_LOOKING_HINT)).toBeNull();
    expect(clearSwarmAwaitWaitHint(SWARM_AWAIT_HINT)).toBeNull();
  });

  it("clears await wait hints when swarm-results session state drains or Stop sticks", () => {
    expect(
      swarmResultsAwaitChromeClear({
        pendingSwarms: false,
        localPendingJobCount: 0,
        userStopped: false,
        cancelArmed: false,
      }),
    ).toEqual({ clearAwaitStatus: true, clearWaitHint: true });
    expect(
      swarmResultsAwaitChromeClear({
        pendingSwarms: true,
        localPendingJobCount: 1,
        userStopped: true,
        cancelArmed: false,
      }),
    ).toEqual({ clearAwaitStatus: true, clearWaitHint: true });
    expect(
      swarmResultsAwaitChromeClear({
        pendingSwarms: true,
        localPendingJobCount: 0,
        userStopped: false,
        cancelArmed: false,
      }),
    ).toEqual({ clearAwaitStatus: false, clearWaitHint: false });
  });

  it("fences trailing getSessionState apply so late session-A poll cannot mutate B", () => {
    const pollFromA = {
      pollGen: 1,
      currentGen: 2,
      pollSessionId: "session-a",
      cachedSessionId: "session-b",
      activeSessionId: "session-b",
    };
    expect(shouldApplySwarmLiveMerge(pollFromA)).toBe(false);
    // When the fence rejects, Conversation must skip setBackendPendingSwarms /
    // awaiting_swarm→done / Looking… clear for the stale poll.
    expect(shouldApplySwarmLiveMerge({
      pollGen: 2,
      currentGen: 2,
      pollSessionId: "session-b",
      cachedSessionId: "session-b",
      activeSessionId: "session-b",
    })).toBe(true);
  });
});
