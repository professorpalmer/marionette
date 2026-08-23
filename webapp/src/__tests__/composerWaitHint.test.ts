import {
  formatDriverFailureWaitHint,
  isProviderFailureWaitHint,
  waitHintForBusyProgress,
} from "../lib/composerWaitHint";
import { deriveBusyProgress } from "../lib/turnProgress";

type StreamStatus = "error" | "idle" | "done" | "thinking" | "awaiting_swarm" | "executing" | "streaming";
import { createApplyStreamEvent } from "../components/conversation/streamEventHandler";
import type { Item } from "../components/TranscriptList";
import { describe, expect, it } from "vitest";

describe("composerWaitHint", () => {
  it("recognizes driver failure notices", () => {
    const hint = formatDriverFailureWaitHint("openrouter:deepseek");
    expect(hint).toBe("driver openrouter:deepseek failed");
    expect(isProviderFailureWaitHint(hint)).toBe(true);
    expect(isProviderFailureWaitHint("Still working…")).toBe(false);
  });

  it("suppresses stale failure chrome after the turn shows live progress", () => {
    const hint = formatDriverFailureWaitHint("openrouter:deepseek");
    expect(
      waitHintForBusyProgress(hint, { hasSignal: true, turnFailed: false }),
    ).toBeNull();
    expect(
      waitHintForBusyProgress(hint, { hasSignal: false, turnFailed: false }),
    ).toBe(hint);
    expect(
      waitHintForBusyProgress(hint, { hasSignal: true, turnFailed: true }),
    ).toBe(hint);
  });
});

describe("deriveBusyProgress recover-after-fail", () => {
  it("does not keep driver failure in the busy label after tokens arrive", () => {
    const failureHint = formatDriverFailureWaitHint("openrouter:glm-5");
    const waiting = deriveBusyProgress(
      [
        {
          kind: "msg",
          msg: { role: "assistant", text: "Recovered answer", streaming: true },
        },
      ],
      "streaming",
      null,
      { modelLabel: "openrouter:glm-5", waitHint: failureHint },
    );
    expect(waiting.label).not.toMatch(/failed/i);
    expect(waiting.pill).not.toMatch(/failed/i);
  });

  it("keeps driver failure in the busy label when the turn settles in error", () => {
    const failureHint = formatDriverFailureWaitHint("openrouter:glm-5");
    const errored = deriveBusyProgress(
      [{ kind: "msg", msg: { role: "user", text: "go" } }],
      "error",
      null,
      { modelLabel: "openrouter:glm-5", waitHint: failureHint },
    );
    expect(errored.label).toMatch(/failed/i);
  });
});

function makeWaitHintApplyDeps() {
  const state = {
    items: [{ kind: "msg", msg: { role: "user", text: "go" } }] as Item[],
    itemsRef: { current: [] as Item[] },
    typeBufRef: { current: "" },
    waitHint: null as string | null,
    status: "thinking" as StreamStatus,
  };
  state.itemsRef.current = state.items;
  const pendingJobIdsRef = { current: [] as string[] };
  const setItems = (updater: Item[] | ((prev: Item[]) => Item[])) => {
    const next = typeof updater === "function" ? updater(state.items) : updater;
    state.items = next;
    state.itemsRef.current = next;
  };
  return {
    state,
    apply: createApplyStreamEvent({
      setCompactingStatus: () => {},
      setItems,
      setDistillNotice: () => {},
      setWikiPrepared: () => {},
      setMemoryProposals: () => {},
      setWaitHint: (value) => {
        state.waitHint = typeof value === "function" ? value(state.waitHint) : value;
      },
      setStatus: (value) => {
        state.status = typeof value === "function" ? value(state.status) : value;
      },
      setTurnOpen: () => {},
      setPendingJobIds: () => {},
      pendingJobIdsRef,
      setSafeTimeout: () => {},
      itemsRef: state.itemsRef,
      planTurnRef: { current: false },
      turnSettledRef: { current: false },
      resumeQueuedRef: { current: false },
      typeBufRef: state.typeBufRef,
      flushTypewriter: () => {},
      startTypewriter: () => {},
      appendStreamingText: () => {},
      setCard: () => {},
      onArtifacts: () => {},
      onJobChange: () => {},
      handleSwarmResult: () => {},
      refreshQueue: () => {},
      fetchContextUsage: () => {},
    }),
  };
}

describe("createApplyStreamEvent provider failure wait hints", () => {
  const failureNotice = formatDriverFailureWaitHint("openrouter:deepseek");

  it("clears driver failure wait hints after message_delta recovery in the same turn", () => {
    const { state, apply } = makeWaitHintApplyDeps();
    apply({ kind: "notice", data: { kind: "wait", message: failureNotice } });
    expect(state.waitHint).toBe(failureNotice);
    apply({ kind: "message_delta", data: { text: "Recovered", stream_id: "a1", channel: "answer" } });
    expect(state.waitHint).toBeNull();
    const busy = deriveBusyProgress(state.items, "streaming", null, {
      modelLabel: "openrouter:deepseek",
      waitHint: state.waitHint,
    });
    expect(busy.label).not.toMatch(/failed/i);
  });

  it("keeps driver failure wait hints when the turn settles in error", () => {
    const { state, apply } = makeWaitHintApplyDeps();
    apply({ kind: "notice", data: { kind: "wait", message: failureNotice } });
    apply({ kind: "error", data: { error: "provider rejected" } });
    expect(state.waitHint).toBe(failureNotice);
    expect(state.status).toBe("error");
  });
});
