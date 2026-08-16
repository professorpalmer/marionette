import { describe, expect, it } from "vitest";
import {
  clearSwarmAwaitWaitHint,
  hasLiveBackgroundJobIds,
  PILOT_LOOKING_HINT,
  pilotResumePollAction,
  pruneTerminalJobIds,
  seedPendingJobIdsFromHydrate,
  hydratePendingJobIdsAfterReload,
  pendingJobIdsFromSwarmLive,
  sessionStateShowsAwaitingSwarm,
  shouldHoldSwarmAwaitChrome,
  SWARM_AWAIT_HINT,
  swarmResultsAwaitChromeClear,
  terminalJobIdsFromSwarmLive,
  terminalJobIdsNeedingResultRecovery,
  triggerResumeGate,
  waitHintForAssistantDone,
} from "../components/conversation/swarmPoll";
import { runnerBusySwitchDecision } from "../components/conversation/sessionHydrate";
import { shouldApplySwarmLiveMerge } from "../components/conversation/streamApply";
import { deriveBusyProgress, shouldShowBusyFooter } from "../lib/turnProgress";
import {
  derivePillBusyDetail,
  derivePillStatus,
  isPilotBusy,
  isSwarmPausePoint,
} from "../components/conversation/pillStatus";
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

  it("awaiting restore rearms backendPendingSwarms so swarmResultsPending enables", () => {
    // Mirror Conversation activeSessionId clear + useSessionSwitch applyRunnerBusy /
    // useRunnersBusyPoll: chrome restore must not leave the results poller dark
    // while pendingJobIds are still empty (pre-hydrate seed).
    const pendingJobIdsAfterSwitchClear: string[] = [];
    const sessionState = {
      state: "awaiting_swarm" as const,
      pending_swarms: true,
    };
    const userStopped = false;
    const awaiting = sessionStateShowsAwaitingSwarm({
      state: sessionState.state,
      pendingSwarms: sessionState.pending_swarms,
      userStopped,
    });
    expect(awaiting).toBe(true);
    expect(
      runnerBusySwitchDecision({
        runnerState: "idle",
        localStreamActive: false,
        switchedSession: true,
        sessionState: sessionState.state,
        pendingSwarms: sessionState.pending_swarms,
      }).kind,
    ).toBe("awaiting");
    // applyRunnerBusy / busy-poll set this when awaiting (not only the peek).
    const backendPendingSwarms = awaiting;
    const swarmResultsPending =
      pendingJobIdsAfterSwitchClear.length > 0 || backendPendingSwarms;
    expect(swarmResultsPending).toBe(true);
    // Idle finalize / busy-without-pending must clear (no sticky-true).
    expect(
      sessionStateShowsAwaitingSwarm({
        state: "idle",
        pendingSwarms: false,
        userStopped: false,
      }),
    ).toBe(false);
    // Stop suppresses rearm.
    expect(
      sessionStateShowsAwaitingSwarm({
        state: "awaiting_swarm",
        pendingSwarms: true,
        userStopped: true,
      }),
    ).toBe(false);
  });

  it("seeds pendingJobIds from unresolved swarm_pending cards only", () => {
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
    expect(seedPendingJobIdsFromHydrate({ items })).toEqual(["local-bf1b30f4"]);
    expect(
      seedPendingJobIdsFromHydrate({
        items: [msg("user", "hi")],
      }),
    ).toEqual([]);
  });

  it("does not re-arm Still working from historical session job_ids after reload", () => {
    const items: Item[] = [
      msg("user", "compare the three"),
      msg("assistant", "Practical comparison"),
      {
        kind: "swarm_pending",
        job_ids: ["job_old"],
        objective: "search",
        status: "running",
        resolved: false,
        terminal_job_ids: [],
      },
    ];
    const liveDone = [
      { job_id: "job_old", status: "cancelled" },
      { id: "job_from_transcript", status: "completed" },
      { job_id: "job_interrupted", status: "interrupted" },
    ];
    expect(
      hydratePendingJobIdsAfterReload({ liveJobs: liveDone, items }),
    ).toEqual([]);
    expect(
      hydratePendingJobIdsAfterReload({ liveJobs: [], items }),
    ).toEqual([]);
    expect(
      shouldHoldSwarmAwaitChrome({
        pendingJobIds: hydratePendingJobIdsAfterReload({ liveJobs: [], items }),
        backendPendingSwarms: false,
        userStopped: false,
      }),
    ).toBe(false);
    expect(
      hydratePendingJobIdsAfterReload({ liveJobs: null, items }),
    ).toEqual(["job_old"]);
  });

  it("keeps only live non-terminal jobs after reload", () => {
    expect(
      pendingJobIdsFromSwarmLive([
        { job_id: "job_alive", status: "running" },
        { job_id: "job_done", status: "completed" },
        { job_id: "job_complete", status: "complete" },
        { id: "local-swarm-skip", status: "pending" },
        { job_id: "job_queued", status: "queued" },
      ]),
    ).toEqual(["job_alive", "job_queued"]);
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

  it("derivePillStatus prefers awaiting_swarm over sticky liveInvestigation", () => {
    // R15: hold-extended agentLoopOpen can leave liveInvestigation sticky at
    // pause-point — awaiting_swarm must still win (Still working…, not Investigating…).
    const pill = derivePillStatus({
      transcriptStale: false,
      answerChromeIdle: false,
      liveInvestigation: true,
      turnOpen: false,
      status: "idle",
      awaitingSwarm: true,
      agentLoopOpen: true,
    });
    expect(pill).toBe("awaiting_swarm");
    expect(statusPillLabel(pill)).toBe("Still working…");
    expect(
      derivePillBusyDetail({
        liveInvestigation: true,
        pillStatus: pill,
        agentLoopOpen: true,
      }),
    ).toBe("Still working…");
  });

  it("hold+idle pause matches Explored + Still working (pilotBusy gate)", () => {
    // Matches TranscriptList.pausePoint: hold while idle → pause chrome.
    expect(
      isSwarmPausePoint({
        status: "idle",
        holdSwarmAwait: true,
        turnOpen: false,
      }),
    ).toBe(true);
    expect(isPilotBusy(false, "idle")).toBe(false);

    const pill = derivePillStatus({
      transcriptStale: false,
      answerChromeIdle: false,
      liveInvestigation: true, // sticky via hold-extended agentLoopOpen
      turnOpen: false,
      status: "idle",
      awaitingSwarm: isSwarmPausePoint({
        status: "idle",
        holdSwarmAwait: true,
        turnOpen: false,
      }),
      agentLoopOpen: true, // bare holdSwarmAwait still latches Stop/Steer
    });
    expect(pill).toBe("awaiting_swarm");
    expect(
      derivePillBusyDetail({
        liveInvestigation: true,
        pillStatus: pill,
        agentLoopOpen: true,
      }),
    ).toBe("Still working…");
  });

  it("hold+thinking mid-turn keeps Investigating on pill when pilotBusy", () => {
    // Bare hold must not short-circuit StatusPill to Still working… mid-turn.
    expect(isPilotBusy(false, "thinking")).toBe(true);
    expect(
      isSwarmPausePoint({
        status: "thinking",
        holdSwarmAwait: true,
        turnOpen: false,
      }),
    ).toBe(false);
    expect(
      isSwarmPausePoint({
        status: "thinking",
        holdSwarmAwait: true,
        turnOpen: true,
      }),
    ).toBe(false);

    const pill = derivePillStatus({
      transcriptStale: false,
      answerChromeIdle: false,
      liveInvestigation: true,
      turnOpen: true,
      status: "thinking",
      awaitingSwarm: isSwarmPausePoint({
        status: "thinking",
        holdSwarmAwait: true,
        turnOpen: true,
      }),
      agentLoopOpen: true,
    });
    expect(pill).toBe("investigating");
    expect(statusPillLabel(pill)).toBe("Investigating…");
    expect(
      derivePillBusyDetail({
        liveInvestigation: true,
        pillStatus: pill,
        agentLoopOpen: true,
      }),
    ).toBe("Investigating…");
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
    // Drained + cancelArmed must still clear — cancelArmed only queues resume.
    expect(
      swarmResultsAwaitChromeClear({
        pendingSwarms: false,
        localPendingJobCount: 0,
        userStopped: false,
        cancelArmed: true,
      }),
    ).toEqual({ clearAwaitStatus: true, clearWaitHint: true });
  });

  it("prunes terminal swarm/live job ids from pendingJobIds", () => {
    expect(
      terminalJobIdsFromSwarmLive([
        { job_id: "job_done", status: "completed" },
        { job_id: "job_complete", status: "complete" },
        { job_id: "job_alive", status: "running" },
        { id: "local-x", status: "failed" },
        { job_id: "job_cancel", status: "cancelled" },
        { job_id: "job_interrupted", status: "interrupted" },
      ]),
    ).toEqual(["job_done", "job_complete", "local-x", "job_cancel", "job_interrupted"]);
    expect(
      pruneTerminalJobIds(
        ["job_done", "job_alive", "local-x"],
        ["job_done", "local-x"],
      ),
    ).toEqual(["job_alive"]);
    expect(pruneTerminalJobIds(["job_alive"], [])).toEqual(["job_alive"]);
    expect(pruneTerminalJobIds([], ["job_done"])).toEqual([]);
  });

  it("recovers a missed terminal result through the durable drain exactly once", () => {
    const pending = [{
      kind: "swarm_pending",
      job_ids: ["job_failed"],
      objective: "repair terminal continuation",
      status: "running",
      resolved: false,
    }] as Item[];

    expect(terminalJobIdsNeedingResultRecovery(
      ["job_failed"],
      ["job_failed", "job_other_session"],
      pending,
    )).toEqual(["job_failed"]);

    const delivered = [...pending, {
      kind: "swarm_result",
      job_id: "job_failed",
      objective: "repair terminal continuation",
      applied: false,
      files: [],
      summary: "Background work failed.",
      error: "worker died",
    }] as Item[];
    expect(terminalJobIdsNeedingResultRecovery(
      ["job_failed"],
      ["job_failed"],
      delivered,
    )).toEqual([]);
    expect(terminalJobIdsNeedingResultRecovery(
      ["job_failed"],
      ["job_other_session"],
      pending,
    )).toEqual([]);
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
