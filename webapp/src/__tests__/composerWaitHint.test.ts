import {
  formatDriverFailureWaitHint,
  isProviderFailureWaitHint,
  noticeShouldLatchWaitHint,
  waitHintForBusyProgress,
} from "../lib/composerWaitHint";
import { deriveBusyProgress } from "../lib/turnProgress";
import { CONVERSATION_TURN_FAILURE } from "../lib/operationalDiagnostic";
import { getActiveDiagnostic, resetDiagnosticBus } from "../lib/operationalDiagnosticBus";
import { syncConversationTurnFailureDiagnostic } from "../lib/operationalRecovery";
import { setCorrelationId } from "../lib/correlationId";
import { createApplyStreamEvent } from "../components/conversation/streamEventHandler";
import type { Item } from "../components/TranscriptList";
import { describe, expect, it } from "vitest";

type StreamStatus = "error" | "idle" | "done" | "thinking" | "awaiting_swarm" | "executing" | "streaming";

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

  it("does not latch provider failure notices after live progress", () => {
    const sidecar = "driver openrouter:deepseek/deepseek-v4-flash-vision-exp failed";
    expect(noticeShouldLatchWaitHint(sidecar, { hasLiveProgress: false, turnSettled: false })).toBe(true);
    expect(noticeShouldLatchWaitHint(sidecar, { hasLiveProgress: true, turnSettled: false })).toBe(false);
    expect(noticeShouldLatchWaitHint(sidecar, { hasLiveProgress: false, turnSettled: true })).toBe(false);
    expect(noticeShouldLatchWaitHint("Provider still working", { hasLiveProgress: true, turnSettled: false })).toBe(true);
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
    turnSettled: false,
  };
  state.itemsRef.current = state.items;
  const pendingJobIdsRef = { current: [] as string[] };
  const turnSettledRef = { current: false };
  const setItems = (updater: Item[] | ((prev: Item[]) => Item[])) => {
    const next = typeof updater === "function" ? updater(state.items) : updater;
    state.items = next;
    state.itemsRef.current = next;
  };
  const recordTurnSettle = (settle: Parameters<typeof syncConversationTurnFailureDiagnostic>[0]) => {
    state.turnSettled = true;
    turnSettledRef.current = true;
    syncConversationTurnFailureDiagnostic(settle);
  };
  return {
    state,
    turnSettledRef,
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
      turnSettledRef,
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
      recordTurnSettle,
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

describe("recovered vision sidecar driver miss is not a failed turn", () => {
  const sidecar = "driver openrouter:deepseek/deepseek-v4-flash-vision-exp failed";

  it("does not latch wait-hint or Error/Trace after a successful turn", () => {
    resetDiagnosticBus();
    setCorrelationId("trace-sidecar-ok");
    const { state, apply } = makeWaitHintApplyDeps();
    apply({ kind: "notice", data: { kind: "wait", message: sidecar } });
    expect(state.waitHint).toBe(sidecar);
    apply({ kind: "message_delta", data: { text: "The turn worked.", stream_id: "a1", channel: "answer" } });
    expect(state.waitHint).toBeNull();
    apply({ kind: "notice", data: { kind: "wait", message: sidecar } });
    expect(state.waitHint).toBeNull();
    apply({ kind: "assistant_done", data: { stop_cause: "natural" } });
    expect(state.waitHint).toBeNull();
    expect(state.status).toBe("done");
    expect(getActiveDiagnostic()).toBeNull();
    const busy = deriveBusyProgress(state.items, state.status, null, {
      modelLabel: "openrouter:deepseek/deepseek-v4-flash-vision-exp",
      waitHint: state.waitHint,
    });
    expect(busy.label).not.toMatch(/failed/i);
    setCorrelationId("");
  });

  it("does not publish Trace/Retry when the sidecar arrives as error then the turn succeeds", () => {
    resetDiagnosticBus();
    setCorrelationId("trace-sidecar-error-kind");
    const { state, apply } = makeWaitHintApplyDeps();
    apply({ kind: "error", data: { error: sidecar } });
    expect(state.status).not.toBe("error");
    expect(getActiveDiagnostic()).toBeNull();
    apply({ kind: "message_delta", data: { text: "The turn worked.", stream_id: "a1", channel: "answer" } });
    apply({ kind: "assistant_done", data: { stop_cause: "natural" } });
    expect(state.waitHint).toBeNull();
    expect(state.status).toBe("done");
    expect(getActiveDiagnostic()).toBeNull();
    setCorrelationId("");
  });

  it("still shows Trace+Retry on a genuinely failed turn", () => {
    resetDiagnosticBus();
    setCorrelationId("trace-turn-fail");
    const { state, apply } = makeWaitHintApplyDeps();
    apply({ kind: "notice", data: { kind: "wait", message: sidecar } });
    apply({ kind: "error", data: { error: "all routes exhausted" } });
    expect(state.waitHint).toBe(sidecar);
    expect(state.status).toBe("error");
    const diag = getActiveDiagnostic();
    expect(diag?.code).toBe(CONVERSATION_TURN_FAILURE);
    expect(diag?.recovery).toEqual({ kind: "retry", label: "Retry" });
    expect(diag?.correlationId).toBe("trace-turn-fail");
    setCorrelationId("");
  });
});
