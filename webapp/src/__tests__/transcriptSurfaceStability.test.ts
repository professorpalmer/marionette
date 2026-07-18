import { describe, expect, it } from "vitest";
import {
  appendActionStartCard,
  appendStreamingTextToItems,
  assistantProseCovers,
  ensureAssistantStreamingBubble,
  finalizeOpenPilotBubble,
  finalizePilotMessage,
  flushTypewriterBuffer,
  mergeTranscriptItems,
  RUNNERS_IDLE_CONFIRM_POLLS,
  runnersBusyTickDecision,
  sealOpenStreamSurfaces,
  sealedAssistantCoversDelta,
  upsertStreamingThinking,
  upsertToolPrep,
} from "../components/Conversation";
import {
  collectIntermediateAssistantItems,
  type Item,
} from "../components/TranscriptList";
import { createApplyStreamEvent } from "../components/conversation/streamEventHandler";

/** Compact kind fingerprint for order/stability assertions (ignores tool_prep chrome). */
function surfaceKinds(items: Item[]): string[] {
  return items
    .filter((it) => it.kind !== "tool_prep")
    .map((it) => {
      if (it.kind === "msg") return `msg:${it.msg.role}${it.msg.streaming ? "*" : ""}`;
      if (it.kind === "thinking") return `thinking${it.streaming ? "*" : ""}`;
      if (it.kind === "card") return `card:${it.card.kind || "tool"}`;
      return it.kind;
    });
}

function thinkingTexts(items: Item[]): string[] {
  return items
    .filter((it): it is Extract<Item, { kind: "thinking" }> => it.kind === "thinking")
    .map((it) => it.text);
}

function assistantTexts(items: Item[]): string[] {
  return items
    .filter(
      (it): it is Extract<Item, { kind: "msg" }> =>
        it.kind === "msg" && it.msg.role === "assistant",
    )
    .map((it) => it.msg.text);
}

function makeApplyDeps(opts: {
  items: Item[];
  itemsRef: { current: Item[] };
  typeBufRef: { current: string };
  order?: string[];
}) {
  const order = opts.order;
  const setItems = (updater: Item[] | ((prev: Item[]) => Item[])) => {
    order?.push("setItems");
    const prev = opts.items;
    const next = typeof updater === "function" ? updater(prev) : updater;
    opts.items = next;
    opts.itemsRef.current = next;
  };
  const appendStreamingText = (chunk: string) => {
    if (!chunk) return;
    setItems((p) => appendStreamingTextToItems(p, chunk));
  };
  const flushTypewriter = () => {
    order?.push("flush");
    flushTypewriterBuffer(
      {
        typeBufRef: opts.typeBufRef,
        typeRafRef: { current: null },
        typeDoneRef: { current: false },
      },
      appendStreamingText,
      () => {},
    );
  };
  return {
    setCompactingStatus: () => {},
    setItems,
    setDistillNotice: () => {},
    setWikiPrepared: () => {},
    setMemoryProposals: () => {},
    setWaitHint: () => {},
    setStatus: () => {},
    setTurnOpen: () => {},
    setPendingJobIds: () => {},
    pendingJobIdsRef: { current: [] as string[] },
    setSafeTimeout: () => {},
    itemsRef: opts.itemsRef,
    planTurnRef: { current: false },
    turnSettledRef: { current: false },
    resumeQueuedRef: { current: false },
    typeBufRef: opts.typeBufRef,
    flushTypewriter,
    startTypewriter: () => {},
    appendStreamingText,
    setCard: () => {},
    onArtifacts: () => {},
    onJobChange: () => {},
    handleSwarmResult: () => {},
    refreshQueue: () => {},
    fetchContextUsage: () => {},
    get items() {
      return opts.items;
    },
  };
}

describe("transcript surface stability (no mid-turn reclassification)", () => {
  it("keeps kinds/order stable across thinking → message → thinking → tool → action → message", () => {
    let items: Item[] = [{ kind: "msg", msg: { role: "user", text: "go" } }];

    // thinking_delta
    items = upsertStreamingThinking(finalizeOpenPilotBubble(items), "reason-A ");
    expect(surfaceKinds(items)).toEqual(["msg:user", "thinking*"]);
    const afterThink1 = surfaceKinds(items);
    const think1Text = thinkingTexts(items)[0];

    // message_delta (seals thinking, opens assistant bubble)
    items = ensureAssistantStreamingBubble(items);
    items = appendStreamingTextToItems(items, "narration-B");
    expect(surfaceKinds(items)).toEqual(["msg:user", "thinking", "msg:assistant*"]);
    expect(thinkingTexts(items)).toEqual([think1Text]);
    expect(assistantTexts(items)).toEqual(["narration-B"]);
    // First thinking row stays finalized in place (only the streaming flag drops).
    expect(afterThink1).toEqual(["msg:user", "thinking*"]);

    // thinking_delta again (seals pilot bubble, APPENDS new thinking row)
    items = upsertStreamingThinking(finalizeOpenPilotBubble(items), "reason-C");
    expect(surfaceKinds(items)).toEqual([
      "msg:user",
      "thinking",
      "msg:assistant",
      "thinking*",
    ]);
    expect(thinkingTexts(items)).toEqual(["reason-A ", "reason-C"]);
    expect(assistantTexts(items)).toEqual(["narration-B"]);
    const afterThink2 = surfaceKinds(items);

    // tool_prep / tool_call (seal surfaces; tool card holds only tool data)
    items = upsertToolPrep(sealOpenStreamSurfaces(items), "Read", {
      id: "call-1",
      goal: "foo.ts",
    });
    expect(surfaceKinds(items)).toEqual([
      "msg:user",
      "thinking",
      "msg:assistant",
      "thinking",
      "card:read_file",
    ]);
    expect(thinkingTexts(items)).toEqual(["reason-A ", "reason-C"]);
    expect(assistantTexts(items)).toEqual(["narration-B"]);
    const prepCard = items.find(
      (it) => it.kind === "card" && it.card.id === "tool-prep:call-1",
    ) as Extract<Item, { kind: "card" }>;
    expect(prepCard.card.goal).toBe("foo.ts");
    expect(prepCard.card.goal).not.toContain("reason");
    expect(prepCard.card.goal).not.toContain("narration");
    const afterTool = surfaceKinds(items);

    // action_start (real card; prior fragments unchanged)
    items = appendActionStartCard(items, {
      id: "action-1",
      goal: "foo.ts",
      kind: "read_file",
    });
    expect(surfaceKinds(items)).toEqual([
      "msg:user",
      "thinking",
      "msg:assistant",
      "thinking",
      "card:read_file",
    ]);
    expect(thinkingTexts(items)).toEqual(["reason-A ", "reason-C"]);
    expect(assistantTexts(items)).toEqual(["narration-B"]);
    // Prefix through sealed thinking rows is unchanged across tool_prep → action_start
    expect(afterTool.slice(0, 4)).toEqual(afterThink2.slice(0, 4).map((k) =>
      k.endsWith("*") ? k.slice(0, -1) : k,
    ));

    // final message event
    items = finalizePilotMessage(items, "final-D");
    expect(surfaceKinds(items)).toEqual([
      "msg:user",
      "thinking",
      "msg:assistant",
      "thinking",
      "card:read_file",
      "msg:assistant",
    ]);
    expect(thinkingTexts(items)).toEqual(["reason-A ", "reason-C"]);
    expect(assistantTexts(items)).toEqual(["narration-B", "final-D"]);
  });

  it("does not reopen an earlier thinking row after an assistant bubble", () => {
    let items: Item[] = upsertStreamingThinking([], "first");
    items = ensureAssistantStreamingBubble(items);
    items = appendStreamingTextToItems(items, "bubble");
    // Simulate a stale streaming flag on the early thinking row (race).
    const stale: Item[] = items.map((it) =>
      it.kind === "thinking"
        ? { ...it, streaming: true }
        : it,
    );
    const next = upsertStreamingThinking(stale, " second-phase");
    expect(thinkingTexts(next)).toEqual(["first", " second-phase"]);
    expect(assistantTexts(next)).toEqual(["bubble"]);
    expect(surfaceKinds(next)).toEqual(["thinking", "msg:assistant*", "thinking*"]);
  });

  it("pre-tool assistant bubbles stay non-intermediate when cards arrive later", () => {
    const preTool: Item = {
      kind: "msg",
      msg: { role: "assistant", text: "I will look" },
    };
    const card: Item = {
      kind: "card",
      card: {
        id: "a1",
        goal: "foo.ts",
        cwd: null,
        kind: "read_file",
        running: true,
        open: false,
      },
    };
    const postTool: Item = {
      kind: "msg",
      msg: { role: "assistant", text: "found it", streaming: true },
    };
    const items: Item[] = [
      { kind: "msg", msg: { role: "user", text: "go" } },
      { kind: "thinking", text: "plan", id: "th-1" },
      preTool,
      card,
      postTool,
    ];

    const whileOpen = collectIntermediateAssistantItems(items, true);
    expect(whileOpen.has(preTool)).toBe(false);
    expect(whileOpen.has(postTool)).toBe(true);

    const whenDone = collectIntermediateAssistantItems(items, false);
    expect(whenDone.has(preTool)).toBe(false);
    // Trailing answer with no card after it stands alone once the loop closes.
    expect(whenDone.has(postTool)).toBe(false);
  });

  it("short shared prefix / substring must not cover distinct post-tool narration", () => {
    const sealed = "I will inspect the handler carefully before editing.";
    expect(assistantProseCovers(sealed, "I will")).toBe(false);
    expect(assistantProseCovers(sealed, "the handler")).toBe(false);
    expect(assistantProseCovers(sealed, "carefully")).toBe(false);
    // Substantial proven prefix/suffix fragments still cover (cursor_gap replay).
    expect(assistantProseCovers(sealed, "I will inspect")).toBe(true);
    expect(assistantProseCovers(sealed, "before editing.")).toBe(true);
    expect(assistantProseCovers(sealed, sealed)).toBe(true);

    const durable: Item[] = [
      { kind: "msg", msg: { role: "user", text: "go" } },
      { kind: "msg", msg: { role: "assistant", text: sealed } },
      {
        kind: "card",
        card: {
          id: "a1",
          goal: "handler.ts",
          cwd: null,
          kind: "read_file",
          running: false,
          open: false,
          result: { status: "ok" },
        },
      },
    ];
    expect(sealedAssistantCoversDelta(durable, "I will")).toBe(false);
    expect(sealedAssistantCoversDelta(durable, "the handler")).toBe(false);
    // Distinct post-tool head must open a new bubble, not be dropped.
    let items = appendStreamingTextToItems(durable, "I will");
    expect(assistantTexts(items)).toEqual([sealed, "I will"]);
    items = appendStreamingTextToItems(items, " fix it now.");
    expect(assistantTexts(items)).toEqual([sealed, "I will fix it now."]);
  });

  it("streamed final after a tool never rewrites the sealed pre-tool bubble", () => {
    let items: Item[] = [
      { kind: "msg", msg: { role: "user", text: "go" } },
      {
        kind: "msg",
        msg: { role: "assistant", text: "I will inspect the handler." },
      },
      {
        kind: "card",
        card: {
          id: "a1",
          goal: "foo.ts",
          cwd: null,
          kind: "read_file",
          running: false,
          open: false,
          result: { status: "ok" },
        },
      },
    ];
    // Exact streamed replay of pre-tool text: no-op (no duplicate).
    items = finalizePilotMessage(items, "I will inspect the handler.", {
      streamed: true,
    });
    expect(assistantTexts(items)).toEqual(["I will inspect the handler."]);
    // Longer / overlapping streamed final must APPEND after the card, not grow
    // the pre-tool bubble above it.
    items = finalizePilotMessage(items, "I will fix it.", { streamed: true });
    expect(assistantTexts(items)).toEqual([
      "I will inspect the handler.",
      "I will fix it.",
    ]);
    expect(surfaceKinds(items)).toEqual([
      "msg:user",
      "msg:assistant",
      "card:read_file",
      "msg:assistant",
    ]);
  });

  it("typewriter text then tool_prep leaves one sealed pre-tool bubble", () => {
    let items: Item[] = [
      { kind: "msg", msg: { role: "user", text: "go" } },
    ];
    items = ensureAssistantStreamingBubble(items);
    items = appendStreamingTextToItems(items, "Looking at the read path first.");
    // Flush-before-seal: buffered prose is already in the bubble before seal.
    items = sealOpenStreamSurfaces(items);
    items = upsertToolPrep(items, "Read", { id: "call-1", goal: "foo.ts" });
    expect(assistantTexts(items)).toEqual(["Looking at the read path first."]);
    expect(surfaceKinds(items)).toEqual([
      "msg:user",
      "msg:assistant",
      "card:read_file",
    ]);
    // Streamed final after the tool must not append a duplicate suffix bubble.
    items = finalizePilotMessage(items, "Looking at the read path first.", {
      streamed: true,
    });
    expect(assistantTexts(items)).toEqual(["Looking at the read path first."]);
    expect(surfaceKinds(items).filter((k) => k.startsWith("msg:assistant"))).toEqual([
      "msg:assistant",
    ]);
  });

  it("streamed final after a tool merges into sealed narration (no duplicate)", () => {
    let items: Item[] = [
      { kind: "msg", msg: { role: "user", text: "go" } },
      {
        kind: "msg",
        msg: { role: "assistant", text: "Checking foo.ts next." },
      },
      {
        kind: "card",
        card: {
          id: "a1",
          goal: "foo.ts",
          cwd: null,
          kind: "read_file",
          running: false,
          open: false,
          result: { status: "ok" },
        },
      },
    ];
    items = finalizePilotMessage(items, "Checking foo.ts next.", { streamed: true });
    expect(assistantTexts(items)).toEqual(["Checking foo.ts next."]);
    // A truly new post-tool answer still appends.
    items = finalizePilotMessage(items, "Found the bug on line 12.");
    expect(assistantTexts(items)).toEqual([
      "Checking foo.ts next.",
      "Found the bug on line 12.",
    ]);
  });

  it("cursor_gap replay does not double narration or remove tool rows", () => {
    const durable: Item[] = [
      { kind: "msg", msg: { role: "user", text: "go" } },
      {
        kind: "msg",
        msg: { role: "assistant", text: "I will inspect the handler." },
      },
      {
        kind: "card",
        card: {
          id: "a1",
          goal: "streamEventHandler.ts",
          cwd: null,
          kind: "read_file",
          running: false,
          open: false,
          result: { status: "ok" },
        },
      },
    ];
    expect(sealedAssistantCoversDelta(durable, "I will inspect")).toBe(true);
    expect(sealedAssistantCoversDelta(durable, " the handler.")).toBe(true);
    // Replayed deltas must be no-ops against durable sealed prose.
    let items = appendStreamingTextToItems(durable, "I will inspect");
    items = appendStreamingTextToItems(items, " the handler.");
    expect(assistantTexts(items)).toEqual(["I will inspect the handler."]);
    expect(items.filter((it) => it.kind === "card")).toHaveLength(1);

    const remoteOnlyMsg: Item[] = [
      { kind: "msg", msg: { role: "user", text: "go" } },
      {
        kind: "msg",
        msg: { role: "assistant", text: "I will inspect the handler." },
      },
    ];
    const merged = mergeTranscriptItems(durable, remoteOnlyMsg);
    expect(merged.filter((it) => it.kind === "card")).toHaveLength(1);
    expect(assistantTexts(merged)).toEqual(["I will inspect the handler."]);

    // Exact final replay is also idempotent.
    items = finalizePilotMessage(items, "I will inspect the handler.", {
      streamed: true,
    });
    expect(assistantTexts(items)).toEqual(["I will inspect the handler."]);
  });

  it("completed cards + idle runner settle only after confirmed idle polls", () => {
    const completed: Item[] = [
      { kind: "msg", msg: { role: "user", text: "go" } },
      {
        kind: "card",
        card: {
          id: "a1",
          goal: "foo.ts",
          cwd: null,
          kind: "read_file",
          running: false,
          open: false,
          result: { status: "ok" },
        },
      },
    ];
    // First idle sighting must not finalize (transient false poll).
    expect(
      runnersBusyTickDecision({
        userStopped: false,
        localStreamActive: false,
        runnerBusy: false,
        detachedBusy: true,
        chatEventsPollArmed: false,
        items: completed,
        consecutiveIdlePolls: 1,
      }).kind,
    ).toBe("hold_idle_unconfirmed");
    expect(
      runnersBusyTickDecision({
        userStopped: false,
        localStreamActive: false,
        runnerBusy: false,
        detachedBusy: true,
        chatEventsPollArmed: false,
        items: completed,
        consecutiveIdlePolls: RUNNERS_IDLE_CONFIRM_POLLS,
      }).kind,
    ).toBe("finalize_idle_refresh");

    const running: Item[] = [
      { kind: "msg", msg: { role: "user", text: "go" } },
      {
        kind: "card",
        card: {
          id: "a2",
          goal: "bar.ts",
          cwd: null,
          kind: "read_file",
          running: true,
          open: false,
        },
      },
    ];
    expect(
      runnersBusyTickDecision({
        userStopped: false,
        localStreamActive: false,
        runnerBusy: false,
        detachedBusy: true,
        chatEventsPollArmed: false,
        items: running,
        consecutiveIdlePolls: RUNNERS_IDLE_CONFIRM_POLLS,
      }).kind,
    ).toBe("hold_live_investigation");

    // Sticky between tool batches while the runner is still busy.
    expect(
      runnersBusyTickDecision({
        userStopped: false,
        localStreamActive: false,
        runnerBusy: true,
        detachedBusy: true,
        chatEventsPollArmed: true,
        items: completed,
        consecutiveIdlePolls: 0,
      }).kind,
    ).toBe("skip_disk_while_reattach");
  });

  it("idle false→true flicker does not finalize then re-arm within one confirm window", () => {
    const completed: Item[] = [
      { kind: "msg", msg: { role: "user", text: "go" } },
      {
        kind: "card",
        card: {
          id: "a1",
          goal: "foo.ts",
          cwd: null,
          kind: "read_file",
          running: false,
          open: false,
          result: { status: "ok" },
        },
      },
    ];
    // Poll 1: false idle blip — hold, do not finalize.
    expect(
      runnersBusyTickDecision({
        userStopped: false,
        localStreamActive: false,
        runnerBusy: false,
        detachedBusy: true,
        chatEventsPollArmed: true,
        items: completed,
        consecutiveIdlePolls: 1,
      }).kind,
    ).toBe("hold_idle_unconfirmed");
    // Poll 2: runners busy again — reattach/skip, counter would reset in hook.
    expect(
      runnersBusyTickDecision({
        userStopped: false,
        localStreamActive: false,
        runnerBusy: true,
        detachedBusy: true,
        chatEventsPollArmed: true,
        items: completed,
        consecutiveIdlePolls: 0,
      }).kind,
    ).toBe("skip_disk_while_reattach");
    // After reset, another single idle still cannot finalize.
    expect(
      runnersBusyTickDecision({
        userStopped: false,
        localStreamActive: false,
        runnerBusy: false,
        detachedBusy: true,
        chatEventsPollArmed: true,
        items: completed,
        consecutiveIdlePolls: 1,
      }).kind,
    ).toBe("hold_idle_unconfirmed");
  });

  it("applyStreamEvent flushes typewriter via real buffer before tool_prep seal", () => {
    const order: string[] = [];
    const state = {
      items: [
        { kind: "msg", msg: { role: "user", text: "go" } },
        {
          kind: "msg",
          msg: { role: "assistant", text: "partial", streaming: true },
        },
      ] as Item[],
      itemsRef: {
        current: [] as Item[],
      },
      typeBufRef: { current: " buffered" },
      order,
    };
    state.itemsRef.current = state.items;
    const deps = makeApplyDeps(state);
    const apply = createApplyStreamEvent(deps);

    apply({
      kind: "tool_prep",
      data: { name: "Read", id: "c1", goal: "foo.ts" },
    });
    expect(order[0]).toBe("flush");
    expect(order).toContain("setItems");
    expect(assistantTexts(state.items)).toEqual(["partial buffered"]);
    expect(state.items.some((it) => it.kind === "card")).toBe(true);
    expect(state.typeBufRef.current).toBe("");
  });

  it("cursor-gap multi-frame replay keeps post-tool deltas despite stale itemsRef", () => {
    const durable: Item[] = [
      { kind: "msg", msg: { role: "user", text: "go" } },
      {
        kind: "msg",
        msg: { role: "assistant", text: "I will inspect the handler carefully." },
      },
      {
        kind: "card",
        card: {
          id: "a1",
          goal: "handler.ts",
          cwd: null,
          kind: "read_file",
          running: false,
          open: false,
          result: { status: "ok" },
        },
      },
    ];
    const state = {
      items: [...durable],
      // Stale ref as if useEffect has not run after hydrate+prior frames.
      itemsRef: { current: [...durable] },
      typeBufRef: { current: "" },
    };
    const deps = makeApplyDeps(state);
    const apply = createApplyStreamEvent(deps);

    // Short shared prefix must not be dropped as "already covered".
    apply({ kind: "message_delta", data: { text: "I will" } });
    expect(assistantTexts(state.items)).toEqual([
      "I will inspect the handler carefully.",
      "I will",
    ]);
    // Second frame appends into the open post-tool bubble even if itemsRef
    // was stale at the start of the batch.
    state.itemsRef.current = durable;
    apply({ kind: "message_delta", data: { text: " fix the bug." } });
    expect(assistantTexts(state.items)).toEqual([
      "I will inspect the handler carefully.",
      "I will fix the bug.",
    ]);
    expect(state.items.filter((it) => it.kind === "card")).toHaveLength(1);
  });

  it("assistant_done seals any remaining live streaming surface", () => {
    const state = {
      items: [
        { kind: "msg", msg: { role: "user", text: "go" } },
        {
          kind: "msg",
          msg: { role: "assistant", text: "almost done", streaming: true },
        },
      ] as Item[],
      itemsRef: { current: [] as Item[] },
      typeBufRef: { current: "" },
    };
    state.itemsRef.current = state.items;
    const apply = createApplyStreamEvent(makeApplyDeps(state));

    apply({ kind: "assistant_done", data: {} });
    expect(surfaceKinds(state.items)).toEqual(["msg:user", "msg:assistant"]);
    expect(assistantTexts(state.items)).toEqual(["almost done"]);
  });
});
