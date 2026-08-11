/**
 * Wave 5 — busy UI terminal-state honesty.
 *
 * Covers: long reasoning, provider idle then progress, stream EOF without
 * assistant_done, command timeout, Stop during command, and completed swarm
 * plus idle pilot. Pure helpers + createApplyStreamEvent only — no React mount.
 */
import { describe, expect, it } from "vitest";
import type { Item } from "../components/TranscriptList";
import { createApplyStreamEvent } from "../components/conversation/streamEventHandler";
import {
  applyActionResultCard,
  finalizeOrphanSwarmPills,
  reconcileOrphanInvestigationCards,
  sealOpenStreamSurfaces,
} from "../components/conversation/streamApply";
import {
  STREAM_ABORT_MESSAGE,
  resetTurnSettledOnSessionSwitch,
  shouldRefreshBusyChrome,
  streamOnDoneDecision,
} from "../components/conversation/streamTerminal";
import { isTerminalStreamKind } from "../components/conversation/chatEvents";
import { runnersBusyTickDecision } from "../components/conversation/runnersBusy";
import { derivePillStatus } from "../components/conversation/pillStatus";
import { flushTypewriterBuffer } from "../components/conversation/streamTypewriter";
import { appendStreamingTextToItems } from "../components/conversation/streamBubbles";
import {
  cardEffectivelyRunning,
  cardHasDurableJob,
  deriveBusyProgress,
  shouldShowBusyFooter,
  turnHasLiveInvestigation,
  turnHasVisibleBusySurface,
} from "../lib/turnProgress";

function msg(role: "user" | "assistant", text: string, streaming = false): Item {
  return { kind: "msg", msg: { role, text, streaming } };
}

function makeApplyDeps(opts?: {
  turnSettled?: boolean;
}) {
  const state = {
    items: [msg("user", "go")] as Item[],
    itemsRef: { current: [] as Item[] },
    typeBufRef: { current: "" },
    waitHint: null as string | null,
    status: "thinking" as
      | "idle"
      | "thinking"
      | "executing"
      | "done"
      | "error"
      | "streaming"
      | "awaiting_swarm",
    turnOpen: true,
    turnSettledRef: { current: Boolean(opts?.turnSettled) },
  };
  state.itemsRef.current = state.items;

  const setItems = (updater: Item[] | ((prev: Item[]) => Item[])) => {
    const next = typeof updater === "function" ? updater(state.items) : updater;
    state.items = next;
    state.itemsRef.current = next;
  };
  const appendStreamingText = (chunk: string) => {
    if (!chunk) return;
    setItems((p: Item[]) => appendStreamingTextToItems(p, chunk));
  };
  const flushTypewriter = () => {
    flushTypewriterBuffer(
      {
        typeBufRef: state.typeBufRef,
        typeRafRef: { current: null },
        typeDoneRef: { current: false },
      },
      appendStreamingText,
      () => {},
    );
  };

  const deps = {
    setCompactingStatus: (_v?: string | null) => {},
    setItems,
    setDistillNotice: () => {},
    setWikiPrepared: () => {},
    setMemoryProposals: () => {},
    setWaitHint: (value: string | null | ((prev: string | null) => string | null)) => {
      state.waitHint = typeof value === "function" ? value(state.waitHint) : value;
    },
    setStatus: (
      value:
        | typeof state.status
        | ((prev: typeof state.status) => typeof state.status),
    ) => {
      state.status = typeof value === "function" ? value(state.status) : value;
    },
    setTurnOpen: (value: boolean | ((prev: boolean) => boolean)) => {
      state.turnOpen = typeof value === "function" ? value(state.turnOpen) : value;
    },
    setPendingJobIds: () => {},
    pendingJobIdsRef: { current: [] as string[] },
    setSafeTimeout: () => {},
    itemsRef: state.itemsRef,
    planTurnRef: { current: false },
    turnSettledRef: state.turnSettledRef,
    resumeQueuedRef: { current: false },
    typeBufRef: state.typeBufRef,
    flushTypewriter,
    startTypewriter: () => {},
    appendStreamingText,
    setCard: () => {},
    onArtifacts: () => {},
    onJobChange: () => {},
    handleSwarmResult: () => {},
    refreshQueue: () => {},
    fetchContextUsage: () => {},
  };

  return { state, apply: createApplyStreamEvent(deps) };
}

describe("Wave 5: long reasoning stays busy without idle blink", () => {
  it("keeps thinking chrome while reasoning streams for a long stretch", () => {
    const { state, apply } = makeApplyDeps();
    apply({
      kind: "thinking",
      data: { text: "Step 1 — survey the call graph. ", delta: true, stream_id: "r1" },
    });
    apply({
      kind: "thinking",
      data: { text: "Step 2 — check the busy footer latch. ", delta: true, stream_id: "r1" },
    });
    apply({
      kind: "thinking",
      data: { text: "Step 3 — keep going without a tool yet.", delta: true, stream_id: "r1" },
    });

    expect(state.status).toBe("thinking");
    expect(state.turnOpen).toBe(true);
    expect(state.waitHint).toBeNull();
    const progress = deriveBusyProgress(state.items, state.status, 45_000);
    expect(progress.phase).toBe("thinking");
    expect(progress.label.toLowerCase()).toContain("still working");
    expect(progress.pill.toLowerCase()).toContain("still working");
    expect(progress.pill).not.toBe("idle");
    expect(shouldShowBusyFooter(state.items, state.status)).toBe(true);
  });
});

describe("Wave 5: provider idle then progress", () => {
  it("shows waitHint during genuine silence, then clears on reasoning progress", () => {
    const { state, apply } = makeApplyDeps();
    apply({
      kind: "notice",
      data: { kind: "wait", message: "Provider still working — stream idle" },
    });
    expect(state.waitHint).toBe("Provider still working — stream idle");
    const waiting = deriveBusyProgress(state.items, state.status, 12_000, {
      waitHint: state.waitHint,
    });
    expect(waiting.phase).toBe("waiting");
    expect(waiting.label).toContain("Provider still working");

    apply({
      kind: "thinking",
      data: { text: "Resumed after idle", delta: true, stream_id: "r1" },
    });
    expect(state.waitHint).toBeNull();
    const resumed = deriveBusyProgress(state.items, state.status, 14_000, {
      waitHint: state.waitHint,
    });
    expect(resumed.phase).toBe("thinking");
    expect(resumed.label.toLowerCase()).not.toContain("waiting on provider");
  });

  it("does not let waitHint override a live command card", () => {
    const items: Item[] = [
      msg("user", "run"),
      {
        kind: "card",
        card: {
          id: "c1",
          goal: "pytest -q",
          cwd: null,
          kind: "run_command",
          running: true,
          open: false,
        },
      },
    ];
    const p = deriveBusyProgress(items, "executing", 8_000, {
      waitHint: "Provider still working — stream idle",
    });
    expect(p.phase).toBe("running");
    expect(p.pill.toLowerCase()).toContain("investigating");
    expect(p.label.toLowerCase()).toContain("run command");
    expect(p.label.toLowerCase()).not.toContain("waiting on provider");
    expect(p.label.toLowerCase()).not.toMatch(/\b(running|thinking|streaming)\b/);
  });
});

describe("Wave 5: stream EOF without assistant_done stays honest", () => {
  it("aborts when the stream ends before any terminal settle", () => {
    expect(
      streamOnDoneDecision({ turnSettled: false, userStopped: false }).kind,
    ).toBe("abort_error");
    expect(STREAM_ABORT_MESSAGE).toMatch(/aborted|retry/i);
  });

  it("does not paint a false success after EOF when turn never settled", () => {
    // Mirrors Conversation onDone: abort closes turnOpen and sets error.
    const turnSettled = false;
    const userStopped = false;
    const decision = streamOnDoneDecision({ turnSettled, userStopped });
    expect(decision.kind).toBe("abort_error");
    expect(shouldRefreshBusyChrome({ turnSettled: true })).toBe(false);
  });

  it("silently settles when the answer is already complete (no false abort)", () => {
    expect(
      streamOnDoneDecision({
        turnSettled: false,
        userStopped: false,
        answerComplete: true,
      }).kind,
    ).toBe("done");
  });
});

describe("Wave 5: interrupted / done framing settle turn chrome", () => {
  it("treats interrupted as a terminal stream kind", () => {
    expect(isTerminalStreamKind("interrupted")).toBe(true);
    expect(isTerminalStreamKind("message_delta")).toBe(false);
  });

  it("applies interrupted by settling turnOpen/status/turnSettled", () => {
    const { state, apply } = makeApplyDeps();
    apply({
      kind: "thinking",
      data: { text: "working…", delta: true, stream_id: "t1" },
    });
    expect(state.turnOpen).toBe(true);
    apply({ kind: "interrupted", data: { reason: "session interrupted" } });
    expect(state.turnSettledRef.current).toBe(true);
    expect(state.turnOpen).toBe(false);
    expect(state.status).toBe("idle");
    expect(shouldRefreshBusyChrome({
      turnSettled: state.turnSettledRef.current,
    })).toBe(false);
  });

  it("applies framing-only done by settling chrome when not already settled", () => {
    const { state, apply } = makeApplyDeps();
    apply({
      kind: "message_delta",
      data: { text: "Here is the answer.", delta: true },
    });
    apply({ kind: "done", data: {} });
    expect(state.turnSettledRef.current).toBe(true);
    expect(state.turnOpen).toBe(false);
    expect(state.status).toBe("done");
  });

  it("clears turnSettled on session switch so mid-turn B can refresh busy chrome", () => {
    const turnSettledRef = { current: true };
    resetTurnSettledOnSessionSwitch(turnSettledRef);
    expect(turnSettledRef.current).toBe(false);
    expect(shouldRefreshBusyChrome({ turnSettled: turnSettledRef.current })).toBe(true);
  });
});

describe("Wave 5: command timeout settles the card and chrome", () => {
  it("treats timeout/truncated/ok receipts as terminal for busy surfaces", () => {
    for (const status of ["timeout", "truncated", "ok", "error"] as const) {
      expect(
        cardEffectivelyRunning({
          running: true,
          result: { job_id: "local-cmd-1", status },
        }),
      ).toBe(false);
    }
    expect(
      cardEffectivelyRunning({
        running: true,
        result: {
          job_id: "local-cmd-1",
          status: "pending",
          terminal_receipt: { status: "timeout", summary: "timed out" },
        } as { job_id?: string | null; status?: string | null; terminal_receipt?: unknown },
      }),
    ).toBe(false);
  });

  it("applies a timeout action_result without leaving a permanent running pill", () => {
    const { state, apply } = makeApplyDeps();
    apply({
      kind: "action_start",
      data: { id: "a1", kind: "run_command", goal: "sleep 999" },
    });
    expect(state.status).toBe("executing");
    apply({
      kind: "action_result",
      data: {
        id: "a1",
        kind: "run_command",
        status: "timeout",
        error: "command timed out",
        job_id: "local-cmd-timeout",
        terminal_receipt: { status: "timeout", summary: "timed out" },
      },
    });
    const cardItem = state.items.find((i) => i.kind === "card") as Extract<
      Item,
      { kind: "card" }
    >;
    expect(cardItem.card.running).toBe(false);
    expect(cardEffectivelyRunning(cardItem.card)).toBe(false);
    expect(state.status).toBe("thinking");

    apply({ kind: "assistant_done", data: {} });
    expect(state.turnOpen).toBe(false);
    expect(state.status).toBe("done");
    expect(state.waitHint).toBeNull();
    expect(turnHasLiveInvestigation(state.items, false)).toBe(false);
    expect(
      derivePillStatus({
        transcriptStale: false,
        answerChromeIdle: false,
        liveInvestigation: turnHasLiveInvestigation(state.items, false),
        turnOpen: state.turnOpen,
        status: state.status,
      }),
    ).toBe("done");
  });
});

describe("Wave 5: Stop during command clears Stop/Steer without false success", () => {
  it("mirrors Stop: settle orphans, clear chrome, keep stream EOF recoverable", () => {
    let items: Item[] = [
      msg("user", "validate"),
      {
        kind: "card",
        card: {
          id: "a-run",
          goal: "pytest -q",
          cwd: null,
          kind: "run_command",
          running: true,
          open: false,
        },
      },
      { kind: "thinking", text: "still working", streaming: true },
    ];
    // Same seal → orphan settle order as Conversation.stop / assistant_done.
    items = reconcileOrphanInvestigationCards(
      finalizeOrphanSwarmPills(sealOpenStreamSurfaces(items), []),
      [],
    );
    const cardItem = items.find((i) => i.kind === "card") as Extract<Item, { kind: "card" }>;
    expect(cardItem.card.running).toBe(false);
    expect(cardItem.card.result?.status).toBe("interrupted");

    expect(shouldRefreshBusyChrome({ turnSettled: true, userStopped: true })).toBe(false);
    expect(
      runnersBusyTickDecision({
        userStopped: true,
        localStreamActive: false,
        runnerBusy: true,
        detachedBusy: true,
        chatEventsPollArmed: false,
        items,
      }).kind,
    ).toBe("force_idle");
  });
});

describe("Wave 5: completed swarm plus idle pilot", () => {
  it("keeps durable background jobs visible without pinning foreground chrome", () => {
    const items: Item[] = [
      msg("user", "dispatch"),
      msg("assistant", "Started the background validation."),
      {
        kind: "card",
        card: {
          id: "bg1",
          goal: "pytest -q",
          cwd: null,
          kind: "run_command",
          running: true,
          open: false,
          result: { job_id: "local-cmd-bg", status: "pending" },
        },
      },
      {
        kind: "swarm_pending",
        job_ids: ["job-swarm-1"],
        objective: "audit",
        status: "done",
        resolved: true,
      },
    ];

    expect(cardHasDurableJob((items[2] as Extract<Item, { kind: "card" }>).card)).toBe(true);
    // Pilot closed: durable running job must not keep Investigating/Stop chrome.
    expect(turnHasLiveInvestigation(items, false)).toBe(false);
    expect(turnHasVisibleBusySurface(items)).toBe(false);
    expect(
      runnersBusyTickDecision({
        userStopped: false,
        localStreamActive: false,
        runnerBusy: false,
        detachedBusy: true,
        chatEventsPollArmed: false,
        items,
        consecutiveIdlePolls: 2,
      }).kind,
    ).toBe("finalize_idle_refresh");
    expect(
      derivePillStatus({
        transcriptStale: false,
        answerChromeIdle: false,
        liveInvestigation: turnHasLiveInvestigation(items, false),
        turnOpen: false,
        status: "done",
      }),
    ).toBe("done");

    // While the pilot loop is still open, sticky investigation remains.
    expect(turnHasLiveInvestigation(items, true)).toBe(true);
  });

  it("late action_result after turn settle updates the card without reopening busy", () => {
    const { state, apply } = makeApplyDeps({ turnSettled: true });
    state.status = "done";
    state.turnOpen = false;
    state.items = [
      msg("user", "dispatch"),
      {
        kind: "card",
        card: {
          id: "bg1",
          goal: "pytest -q",
          cwd: null,
          kind: "run_command",
          running: false,
          open: false,
          result: { job_id: "local-cmd-bg", status: "pending" },
        },
      },
    ];
    state.itemsRef.current = state.items;

    apply({
      kind: "action_result",
      data: {
        id: "bg1",
        kind: "run_command",
        status: "ok",
        job_id: "local-cmd-bg",
        terminal_receipt: { status: "completed", summary: "exit 0" },
      },
    });
    expect(state.status).toBe("done");
    expect(state.turnOpen).toBe(false);
    const cardItem = state.items.find((i) => i.kind === "card") as Extract<
      Item,
      { kind: "card" }
    >;
    expect(cardItem.card.result?.status).toBe("ok");
    expect(cardEffectivelyRunning(cardItem.card)).toBe(false);

    // Late wait notices after settle must not resurrect provider-idle chrome.
    apply({
      kind: "notice",
      data: { kind: "wait", message: "Provider still working — stream idle" },
    });
    expect(state.waitHint).toBeNull();
  });

  it("upgrades a pending background receipt via applyActionResultCard", () => {
    const pending: Item[] = [{
      kind: "card",
      card: {
        id: "bg1",
        goal: "pytest -q",
        cwd: null,
        kind: "run_command",
        running: false,
        open: false,
        result: { job_id: "local-cmd-bg", status: "pending" },
      },
    }];
    const next = applyActionResultCard(pending, {
      id: "bg1",
      kind: "run_command",
      status: "timeout",
      job_id: "local-cmd-bg",
      terminal_receipt: { status: "timeout", summary: "timed out" },
    });
    const card = next[0] as Extract<Item, { kind: "card" }>;
    expect(card.card.running).toBe(false);
    expect(card.card.result?.status).toBe("timeout");
    expect(cardEffectivelyRunning(card.card)).toBe(false);
  });
});
