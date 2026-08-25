/**
 * Wave 3 — authoritative turn terminals, recovery, and live-answer chrome.
 */
import { describe, expect, it } from "vitest";
import type { Item } from "../components/TranscriptList";
import {
  collectIntermediateAssistantItems,
  isLiveAnswerAssistant,
  stableItemKey,
} from "../components/TranscriptList";
import { createApplyStreamEvent } from "../components/conversation/streamEventHandler";
import {
  alreadySettledOnDoneStatus,
  resetTurnLifecycleOnSessionSwitch,
  streamOnDoneDecision,
} from "../components/conversation/streamTerminal";
import {
  mergeLocalTurnTerminals,
  mergeTranscriptItems,
  transcriptResponseToItems,
} from "../components/conversation/transcriptItems";
import { appendTurnTerminal } from "../components/conversation/streamApply";
import { explorationShelfAnchorId } from "../lib/turnProgress";
import { isPilotMouthBusy } from "../components/conversation/runnersBusy";
import { appendStreamingTextToItems } from "../components/conversation/streamBubbles";
import { flushTypewriterBuffer } from "../components/conversation/streamTypewriter";
import { staleLocalStreamTickDecision } from "../components/conversation/runnersBusy";
import { isTerminalStreamKind } from "../components/conversation/chatEvents";
import {
  CONTINUE_PROMPT,
  DIRTY_FINISH_BANNER,
  canonicalizeTerminalCause,
  composerBusyDuringSwitch,
  dirtyFinishExplanation,
  hasPartialAssistantAnswer,
  latestUserAsk,
  recoveryControlsAvailable,
  recoveryDispatchAllowed,
  settleFromAssistantDone,
  settleFromAutoHalt,
  settleFromFramingDone,
  settleFromRingReplayDone,
  settleFromStaleLocalAbandon,
  settleFromStreamError,
  settleFromTransportEof,
  terminalCauseCopy,
  type TerminalCause,
  type TurnSettle,
} from "../lib/turnTerminal";

function msg(role: "user" | "assistant", text: string, streaming = false): Item {
  return { kind: "msg", msg: { role, text, streaming } };
}

function makeApplyDeps(opts?: { turnSettled?: boolean }) {
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
    settle: null as TurnSettle | null,
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

  const apply = createApplyStreamEvent({
    setCompactingStatus: () => {},
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
    recordTurnSettle: (settle) => {
      state.settle = settle;
    },
  });

  return { state, apply };
}

function terminalChip(items: Item[]): Extract<Item, { kind: "turn_terminal" }> | undefined {
  return items.find((it) => it.kind === "turn_terminal") as
    | Extract<Item, { kind: "turn_terminal" }>
    | undefined;
}

describe("Wave 3: transport EOF is never a silent success", () => {
  it("message_delta mid-sentence + stream_item_done + EOF is incomplete, never done", () => {
    const { state, apply } = makeApplyDeps();
    apply({ kind: "message_delta", data: { text: "The handler should " } });
    apply({ kind: "stream_item_done", data: { stream_id: "a1" } });
    expect(hasPartialAssistantAnswer(state.items)).toBe(true);
    expect(streamOnDoneDecision({
      turnSettled: state.turnSettledRef.current,
      userStopped: false,
    }).kind).toBe("abort_error");
    apply({ kind: "done", data: {} });
    expect(state.turnSettledRef.current).toBe(true);
    expect(state.turnOpen).toBe(false);
    expect(state.status).toBe("error");
    expect(state.settle?.lifecycle).toBe("settled_incomplete");
    expect(state.settle?.lifecycle).not.toBe("settled_complete");
    const chip = terminalChip(state.items);
    expect(chip?.cause).toBe("provider_eof");
    expect(chip?.text).not.toMatch(/context\s*%/i);
  });

  it("empty stream_id stream_item_done + EOF is also incomplete", () => {
    const { state, apply } = makeApplyDeps();
    apply({ kind: "message_delta", data: { text: "Halfway through a senten" } });
    apply({ kind: "stream_item_done", data: { stream_id: "" } });
    apply({ kind: "done", data: {} });
    expect(state.settle?.lifecycle).toBe("settled_incomplete");
    expect(state.status).toBe("error");
    expect(terminalChip(state.items)?.cause).toBe("provider_eof");
  });

  it("framing kind=done while the stream is active is incomplete", () => {
    const { state, apply } = makeApplyDeps();
    apply({ kind: "message_delta", data: { text: "Still writing" } });
    expect(state.turnOpen).toBe(true);
    apply({ kind: "done", data: {} });
    expect(state.settle?.lifecycle).toBe("settled_incomplete");
    expect(state.status).not.toBe("done");
  });

  it("assistant_done natural is complete; non-natural shows the cause", () => {
    const natural = makeApplyDeps();
    natural.apply({ kind: "message_delta", data: { text: "All set." } });
    natural.apply({ kind: "assistant_done", data: { stop_cause: "natural", finish_reason: "stop" } });
    expect(natural.state.settle?.lifecycle).toBe("settled_complete");
    expect(natural.state.status).toBe("done");
    expect(terminalChip(natural.state.items)).toBeUndefined();

    const length = makeApplyDeps();
    length.apply({ kind: "message_delta", data: { text: "Cut off mid" } });
    length.apply({
      kind: "assistant_done",
      data: { stop_cause: "length", finish_reason: "length" },
    });
    expect(length.state.settle?.lifecycle).toBe("settled_incomplete");
    expect(terminalChip(length.state.items)?.cause).toBe("length");
    expect(terminalChip(length.state.items)?.text).toMatch(/length/i);
    expect(terminalChip(length.state.items)?.text).not.toMatch(/context\s*%/i);
  });
});

describe("Wave 3: abandon / ring replay cannot paint success", () => {
  it("stale-local abandon without assistant_done is incomplete", () => {
    const settle = settleFromStaleLocalAbandon({
      turnSettled: false,
      userStopped: false,
      hasPartialAnswer: true,
    });
    expect(settle.kind).toBe("settle");
    if (settle.kind !== "settle") return;
    expect(settle.lifecycle).toBe("settled_incomplete");
    expect(settle.status).not.toBe("done");
  });

  it("ring replay done without an authoritative terminal is incomplete", () => {
    expect(isTerminalStreamKind("done")).toBe(true);
    const settle = settleFromRingReplayDone({
      turnSettled: false,
      hasPartialAnswer: true,
    });
    expect(settle.kind).toBe("settle");
    if (settle.kind !== "settle") return;
    expect(settle.lifecycle).toBe("settled_incomplete");
  });

  it("already-settled abandon / framing done stay put", () => {
    expect(settleFromStaleLocalAbandon({
      turnSettled: true,
      userStopped: false,
      hasPartialAnswer: true,
    })).toEqual({ kind: "already_settled" });
    expect(settleFromFramingDone({
      turnSettled: true,
      hasPartialAnswer: true,
    })).toEqual({ kind: "already_settled" });
  });

  it("staleLocalStreamTickDecision still abandons a zombie stream", () => {
    expect(staleLocalStreamTickDecision({
      localStreamActive: true,
      userStopped: false,
      runnerBusy: false,
      awaitingSwarm: false,
      turnSettled: false,
      sawRunnerBusyThisStream: true,
      consecutiveIdlePolls: 2,
    }).kind).toBe("abandon");
  });
});

describe("Wave 3: session switch onto running B never flashes Send", () => {
  it("keeps the mouth busy while the target runner is unknown", () => {
    expect(composerBusyDuringSwitch({
      switchPending: true,
      turnOpen: false,
      status: "idle",
      mouthBusy: isPilotMouthBusy(false, "idle"),
    })).toBe(true);
    expect(isPilotMouthBusy(false, "idle", true)).toBe(true);
    expect(isPilotMouthBusy(true, "thinking", false)).toBe(true);
    expect(isPilotMouthBusy(false, "idle", false)).toBe(false);
  });
});

describe("Wave 3: live answer stays outside the investigation fold", () => {
  it("does not absorb a tool-free streaming answer", () => {
    const live: Item = {
      kind: "msg",
      msg: { role: "assistant", text: "Starting the fix…", streaming: true },
    };
    const items: Item[] = [msg("user", "do it"), live];
    expect(isLiveAnswerAssistant(live.msg)).toBe(true);
    const absorbed = collectIntermediateAssistantItems(items, true);
    expect(absorbed.has(live)).toBe(false);
    expect(collectIntermediateAssistantItems(items, false).has(live)).toBe(false);
  });

  it("does not remount a live answer when the turn settles", () => {
    const answer: Item = {
      kind: "msg",
      msg: { role: "assistant", text: "Here is the patch.", streaming: true, channel: "answer" },
    };
    const items: Item[] = [
      msg("user", "patch it"),
      {
        kind: "thinking",
        text: "inspect",
        id: "th-1",
      },
      {
        kind: "card",
        card: {
          id: "c1",
          goal: "a.ts",
          cwd: null,
          kind: "read_file",
          running: false,
          open: false,
          result: { status: "ok" },
        },
      },
      answer,
    ];
    expect(collectIntermediateAssistantItems(items, true).has(answer)).toBe(false);
    const sealed: Item = {
      kind: "msg",
      msg: { ...answer.msg, streaming: false },
    };
    const after: Item[] = [...items.slice(0, -1), sealed];
    expect(collectIntermediateAssistantItems(after, false).has(sealed)).toBe(false);
  });

  it("does not reparent sealed spoken prose into the fold when a later card exists", () => {
    const spoken: Item = {
      kind: "msg",
      msg: { role: "assistant", text: "Here is the patch.", streaming: false },
    };
    const items: Item[] = [
      msg("user", "patch it"),
      {
        kind: "card",
        card: {
          id: "c1",
          goal: "a.ts",
          cwd: null,
          kind: "read_file",
          running: false,
          open: false,
          result: { status: "ok" },
        },
      },
      spoken,
      {
        kind: "card",
        card: {
          id: "c2",
          goal: "b.ts",
          cwd: null,
          kind: "edit_file",
          running: false,
          open: false,
          result: { status: "ok" },
        },
      },
    ];
    expect(isLiveAnswerAssistant(spoken.msg)).toBe(false);
    expect(collectIntermediateAssistantItems(items, true).has(spoken)).toBe(false);
    expect(collectIntermediateAssistantItems(items, false).has(spoken)).toBe(false);
  });
});

describe("Wave 3: Continue / Retry", () => {
  it("disables recovery while busy and requires exactly one dispatch", () => {
    expect(recoveryControlsAvailable("settled_incomplete")).toBe(true);
    expect(recoveryControlsAvailable("aborted")).toBe(true);
    expect(recoveryControlsAvailable("running")).toBe(false);
    expect(recoveryDispatchAllowed({
      composerBusy: true,
      dispatching: false,
      lifecycle: "settled_incomplete",
    })).toBe(false);
    expect(recoveryDispatchAllowed({
      composerBusy: false,
      dispatching: true,
      lifecycle: "settled_incomplete",
    })).toBe(false);
    expect(recoveryDispatchAllowed({
      composerBusy: false,
      dispatching: false,
      lifecycle: "settled_incomplete",
    })).toBe(true);
  });

  it("preserves the latest user ask and continue prompt", () => {
    const items: Item[] = [
      msg("user", "first"),
      msg("assistant", "partial answer that got cut"),
      msg("user", "please finish the auth patch"),
      msg("assistant", "I started by reading"),
    ];
    expect(latestUserAsk(items)).toBe("please finish the auth patch");
    expect(CONTINUE_PROMPT).toMatch(/continue/i);
  });

  it("Retry skips the internal Continue prompt and uses the real ask", () => {
    const items: Item[] = [
      msg("user", "please finish the auth patch"),
      msg("assistant", "I started by reading"),
      msg("user", CONTINUE_PROMPT),
    ];
    expect(latestUserAsk(items)).toBe("please finish the auth patch");
    expect(latestUserAsk(items)).not.toBe(CONTINUE_PROMPT);
  });
});

describe("Wave 3: truthful cause copy", () => {
  const cases: Array<[TerminalCause, RegExp]> = [
    ["length", /length/i],
    ["incomplete", /incomplete/i],
    ["provider_eof", /stream ended|provider/i],
    ["transport_error", /connection lost/i],
    ["turn_budget", /token budget/i],
    ["step_cap", /step limit/i],
    ["stagnation", /no new progress/i],
    ["invalid_tool", /invalid tool/i],
    ["interrupted", /interrupted/i],
  ];
  it("names the real stop cause and never invents context %", () => {
    for (const [cause, re] of cases) {
      const copy = terminalCauseCopy(cause);
      expect(copy).toMatch(re);
      expect(copy).not.toMatch(/context\s*%/i);
    }
    expect(terminalCauseCopy("context_overflow")).toMatch(/context overflow/i);
  });

  it("maps assistant_done causes onto the lifecycle", () => {
    expect(settleFromAssistantDone({ stopCause: "natural" }).lifecycle).toBe("settled_complete");
    expect(settleFromAssistantDone({
      stopCause: "natural",
      incompleteReason: "length",
    }).lifecycle).toBe("settled_incomplete");
    expect(settleFromAssistantDone({ stopCause: "turn_budget" }).lifecycle).toBe("settled_incomplete");
    expect(settleFromTransportEof({
      turnSettled: false,
      userStopped: false,
      hasPartialAnswer: false,
    })).toMatchObject({ lifecycle: "aborted" });
  });
});

describe("Wave 3: transport EOF decision ignores transcript shape", () => {
  it("streamOnDoneDecision has no answerComplete authority", () => {
    expect(streamOnDoneDecision({ turnSettled: false, userStopped: false }).kind).toBe(
      "abort_error",
    );
    expect(streamOnDoneDecision({ turnSettled: true, userStopped: false }).kind).toBe("done");
  });
});

describe("Wave 3 last-mile: fail-closed empty assistant_done", () => {
  it("blank stop_cause stays unspecified / incomplete, never natural", () => {
    expect(canonicalizeTerminalCause("")).toBe("unspecified");
    expect(canonicalizeTerminalCause(null)).toBe("unspecified");
    expect(canonicalizeTerminalCause(undefined)).toBe("unspecified");
    expect(settleFromAssistantDone({ stopCause: "" }).lifecycle).toBe("settled_incomplete");
    expect(settleFromAssistantDone({ stopCause: "" }).cause).toBe("unspecified");
    expect(settleFromAssistantDone({}).lifecycle).toBe("settled_incomplete");
  });

  it("legacy empty assistant_done event stays incomplete", () => {
    const { state, apply } = makeApplyDeps();
    apply({ kind: "message_delta", data: { text: "Halfway" } });
    apply({ kind: "assistant_done", data: {} });
    expect(state.settle?.lifecycle).toBe("settled_incomplete");
    expect(state.settle?.cause).toBe("unspecified");
    expect(state.settle?.lifecycle).not.toBe("settled_complete");
    expect(state.settle?.explanation).toBeNull();
    expect(terminalChip(state.items)).toBeUndefined();
  });
});

describe("Wave 3 last-mile: one terminal explanation", () => {
  it("stream error paints a chip and no assistant error bubble", () => {
    const { state, apply } = makeApplyDeps();
    apply({ kind: "message_delta", data: { text: "cut off" } });
    apply({ kind: "error", data: { error: "backend dropped" } });
    const chips = state.items.filter((it) => it.kind === "turn_terminal");
    const errorBubbles = state.items.filter(
      (it) => it.kind === "msg" && /\[error\]|\[aborted\]/.test(it.msg.text || ""),
    );
    expect(chips).toHaveLength(1);
    expect(errorBubbles).toHaveLength(0);
    expect(chips[0]?.text).toMatch(/backend dropped/);
  });

  it("settleFromStreamError is a single explanation path", () => {
    const settle = settleFromStreamError("backend dropped");
    expect(settle.explanation).toMatch(/backend dropped/);
    expect(settle.lifecycle).toBe("error");
  });

  it("named model terminals are not relabeled as connection lost", () => {
    for (const cause of ["length", "content_filter", "incomplete"] as const) {
      const settle = settleFromStreamError(
        "OpenAI chat finished with finish_reason=" + cause,
        cause,
      );
      expect(settle.cause).toBe(cause);
      expect(settle.lifecycle).toBe("settled_incomplete");
      expect(settle.explanation).not.toMatch(/connection lost|closed before/i);
      expect(recoveryControlsAvailable(settle.lifecycle)).toBe(true);
    }
    const eof = settleFromStreamError("stream ended", "provider_eof");
    expect(eof.cause).toBe("provider_eof");
    expect(eof.explanation).not.toMatch(/connection lost/i);
    expect(recoveryControlsAvailable(eof.lifecycle)).toBe(true);
  });

  it("error event with terminal_cause paints one truthful chip", () => {
    const { state, apply } = makeApplyDeps();
    apply({ kind: "message_delta", data: { text: "partial cut" } });
    apply({
      kind: "error",
      data: {
        error: "Provider stopped: output length limit (finish_reason=length).",
        terminal_cause: "length",
        finish_reason: "length",
      },
    });
    const chips = state.items.filter((it) => it.kind === "turn_terminal");
    const errorBubbles = state.items.filter(
      (it) => it.kind === "msg" && /\[error\]|\[aborted\]/.test(it.msg.text || ""),
    );
    expect(chips).toHaveLength(1);
    expect(errorBubbles).toHaveLength(0);
    expect(chips[0]?.cause).toBe("length");
    expect(chips[0]?.text).toMatch(/length/i);
    expect(chips[0]?.text).not.toMatch(/connection lost|closed before/i);
    expect(state.settle?.cause).toBe("length");
  });

  it("error then framing done keeps the error settle (done is not success)", () => {
    const { state, apply } = makeApplyDeps();
    apply({ kind: "message_delta", data: { text: "partial" } });
    apply({
      kind: "error",
      data: { error: "mid-turn boom", terminal_cause: "transport_error" },
    });
    expect(state.status).toBe("error");
    expect(state.turnSettledRef.current).toBe(true);
    apply({ kind: "done" });
    expect(state.status).toBe("error");
    expect(state.settle?.lifecycle).toBe("error");
    const chips = state.items.filter((it) => it.kind === "turn_terminal");
    expect(chips).toHaveLength(1);
  });
});

describe("Wave 3 last-mile: session switch isolates recovery", () => {
  it("resets lifecycle, cause, and recovery context on switch", () => {
    const lifecycle: { current: string } = { current: "settled_incomplete" };
    const cause: { current: string | null } = { current: "provider_eof" };
    const recoveryDispatchingRef = { current: true };
    const recoveryContextRef = {
      current: { sessionId: "sess-a", generation: 4 },
    };
    resetTurnLifecycleOnSessionSwitch({
      setTurnLifecycle: (next) => { lifecycle.current = next as typeof lifecycle.current; },
      setTerminalCause: (next) => { cause.current = next as typeof cause.current; },
      recoveryDispatchingRef,
      recoveryContextRef,
    });
    expect(lifecycle.current).toBe("settled_complete");
    expect(cause.current).toBeNull();
    expect(recoveryDispatchingRef.current).toBe(false);
    expect(recoveryContextRef.current).toBeNull();
  });

  it("refuses Continue/Retry after a session switch", () => {
    expect(recoveryDispatchAllowed({
      composerBusy: false,
      dispatching: false,
      lifecycle: "settled_incomplete",
      boundSessionId: "sess-a",
      activeSessionId: "sess-b",
      boundGeneration: 3,
      activeGeneration: 3,
    })).toBe(false);
    expect(recoveryDispatchAllowed({
      composerBusy: false,
      dispatching: false,
      lifecycle: "settled_incomplete",
      boundSessionId: "sess-a",
      activeSessionId: "sess-a",
      boundGeneration: 3,
      activeGeneration: 7,
    })).toBe(false);
    expect(recoveryDispatchAllowed({
      composerBusy: false,
      dispatching: false,
      lifecycle: "settled_incomplete",
      boundSessionId: "sess-a",
      activeSessionId: "sess-a",
      boundGeneration: 3,
      activeGeneration: 3,
    })).toBe(true);
  });
});

describe("Wave 3 last-mile: turn_terminal hydrate / merge", () => {
  it("maps turn_terminal display rows and does not coerce them to messages", () => {
    const items = transcriptResponseToItems({
      display: [
        { type: "message", role: "user", text: "go" },
        {
          type: "turn_terminal",
          id: "term-hydrate-1",
          cause: "provider_eof",
          state: "settled_incomplete",
          text: "Provider stream ended before a clean finish.",
        },
      ],
    });
    const chip = terminalChip(items);
    expect(chip?.kind).toBe("turn_terminal");
    expect(chip?.id).toBe("term-hydrate-1");
    expect(items.some((it) => it.kind === "msg" && /Provider stream ended/.test(it.msg.text || ""))).toBe(false);
  });

  it("preserves a local chip when remote hydrate omits it", () => {
    const local: Item[] = [
      msg("user", "go"),
      msg("assistant", "Halfway"),
      {
        kind: "turn_terminal",
        id: "term-local",
        cause: "provider_eof",
        state: "settled_incomplete",
        text: "Provider stream ended before a clean finish.",
      },
    ];
    const remote: Item[] = [
      msg("user", "go"),
      msg("assistant", "Halfway"),
    ];
    const merged = mergeTranscriptItems(local, remote);
    expect(terminalChip(merged)?.id).toBe("term-local");
    expect(mergeLocalTurnTerminals(remote, local)).toHaveLength(3);
  });
});

describe("Wave 3 last-mile: unique keys and shelf identity", () => {
  it("gives each turn_terminal a unique id, not cause+state", () => {
    const first = appendTurnTerminal([], {
      cause: "provider_eof",
      state: "settled_incomplete",
      text: "first",
    });
    const second = appendTurnTerminal(
      [...first, msg("user", "again")],
      { cause: "provider_eof", state: "settled_incomplete", text: "second" },
    );
    const chips = second.filter((it) => it.kind === "turn_terminal") as Array<
      Extract<Item, { kind: "turn_terminal" }>
    >;
    expect(chips).toHaveLength(2);
    expect(chips[0]?.id).toBeTruthy();
    expect(chips[1]?.id).toBeTruthy();
    expect(chips[0]?.id).not.toBe(chips[1]?.id);
    expect(stableItemKey(chips[0]!, 0)).not.toBe(stableItemKey(chips[1]!, 1));
    expect(stableItemKey(chips[0]!, 0)).not.toMatch(/^turn-term-provider_eof-settled_incomplete$/);
  });

  it("keeps the exploration shelf anchored as cards append", () => {
    expect(explorationShelfAnchorId(["r1", "g1"])).toBe(
      explorationShelfAnchorId(["r1", "g1", "r2"]),
    );
    expect(explorationShelfAnchorId(["r1", "g1"])).toBe("expl-shelf-r1");
  });
});

describe("Wave 3 last-mile: auto_halt and onDone-after-error", () => {
  it("auto_halt settles through the typed incomplete lifecycle", () => {
    const { state, apply } = makeApplyDeps();
    apply({ kind: "message_delta", data: { text: "working" } });
    apply({ kind: "auto_halt", data: { reason: "swarm ceiling reached (2/20)", snapshot: {} } });
    expect(state.turnSettledRef.current).toBe(true);
    expect(state.turnOpen).toBe(false);
    expect(state.settle?.lifecycle).toBe("settled_incomplete");
    expect(state.settle?.lifecycle).not.toBe("running");
    expect(state.settle?.cause).not.toBe("natural");
    expect(settleFromAutoHalt("swarm ceiling reached").lifecycle).toBe("settled_incomplete");
    expect(settleFromAutoHalt("natural").cause).not.toBe("natural");
  });

  it("already-settled onDone preserves error and idle chrome", () => {
    expect(alreadySettledOnDoneStatus({
      prev: "error",
      liveJobs: false,
      userStopped: false,
    })).toBe("error");
    expect(alreadySettledOnDoneStatus({
      prev: "idle",
      liveJobs: false,
      userStopped: true,
    })).toBe("idle");
    expect(alreadySettledOnDoneStatus({
      prev: "error",
      liveJobs: true,
      userStopped: false,
    })).toBe("error");
    expect(alreadySettledOnDoneStatus({
      prev: "done",
      liveJobs: false,
      userStopped: false,
    })).toBe("done");
    expect(alreadySettledOnDoneStatus({
      prev: "awaiting_swarm",
      liveJobs: true,
      userStopped: false,
    })).toBe("awaiting_swarm");
  });

  it("error then framing done does not reseal as success", () => {
    const { state, apply } = makeApplyDeps();
    apply({ kind: "error", data: { error: "backend dropped" } });
    expect(state.status).toBe("error");
    expect(state.settle?.lifecycle).toBe("error");
    apply({ kind: "done", data: {} });
    expect(state.status).toBe("error");
    expect(state.settle?.lifecycle).toBe("error");
    expect(state.items.filter((it) => it.kind === "turn_terminal")).toHaveLength(1);
  });
});


describe("dirty-finish chrome decision", () => {
  it("does not paint CAUSE_UNSPECIFIED as the dirty-finish banner", () => {
    expect(terminalCauseCopy("unspecified")).toBe(DIRTY_FINISH_BANNER);
    expect(dirtyFinishExplanation({ cause: "unspecified" })).toBeNull();
    expect(dirtyFinishExplanation({ cause: "unspecified", finishReason: "stop" })).toBeNull();
    expect(dirtyFinishExplanation({ cause: "unspecified", finishReason: "tool_calls" })).toBeNull();
    expect(dirtyFinishExplanation({ cause: "length" })).toMatch(/length/i);
  });

  it("keeps unspecified fail-closed and stays quiet or Continue on stop/tool_calls", () => {
    for (const wire of ["stop", "tool_calls"] as const) {
      const settle = settleFromAssistantDone({
        stopCause: "unspecified",
        finishReason: wire,
      });
      expect(settle.cause).toBe("unspecified");
      expect(settle.lifecycle).toBe("settled_incomplete");
      expect(settle.lifecycle).not.toBe("settled_complete");
      expect(settle.explanation).toBeNull();
      expect(settle.explanation).not.toBe(DIRTY_FINISH_BANNER);
      expect(recoveryControlsAvailable(settle.lifecycle)).toBe(true);
    }
    const blank = settleFromAssistantDone({ stopCause: "", finishReason: "stop" });
    expect(blank.cause).toBe("unspecified");
    expect(blank.explanation).toBeNull();
    expect(recoveryControlsAvailable(blank.lifecycle)).toBe(true);
  });

  it("assistant_done with wire stop/tool_calls does not paint the banner", () => {
    const { state, apply } = makeApplyDeps();
    apply({ kind: "message_delta", data: { text: "Working" } });
    apply({
      kind: "assistant_done",
      data: { stop_cause: "unspecified", finish_reason: "stop" },
    });
    expect(state.settle?.cause).toBe("unspecified");
    expect(state.settle?.lifecycle).toBe("settled_incomplete");
    expect(state.settle?.explanation).toBeNull();
    expect(terminalChip(state.items)).toBeUndefined();
    const itemsText = state.items.map((it) => ("text" in it ? String(it.text || "") : "")).join(" ");
    expect(itemsText).not.toContain(DIRTY_FINISH_BANNER);
  });
});
