import { describe, expect, it } from "vitest";
import {
  hasLiveBackgroundJobIds,
  shouldHoldSwarmAwaitChrome,
  SWARM_AWAIT_HINT,
  waitHintForAssistantDone,
} from "../components/conversation/swarmPoll";
import { deriveBusyProgress, shouldShowBusyFooter } from "../lib/turnProgress";
import { derivePillStatus } from "../components/conversation/pillStatus";
import type { Item } from "../components/TranscriptList";

function msg(role: string, text: string): Item {
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
});
