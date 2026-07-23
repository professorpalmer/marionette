import { describe, expect, it } from "vitest";
import {
  appendNonStreamingThinking,
  appendStreamingTextToItems,
  chatFrameToStreamEvent,
  finalizePilotMessage,
  finalizeStreamingThinking,
  isTrivialAssistantCrumb,
  upsertStreamingThinking,
} from "../components/Conversation";
import {
  activityGroupStableId,
  liveActivityGroupIndex,
} from "../components/TranscriptList";
import type { GroupedItem, Item } from "../components/TranscriptList";
import { createApplyStreamEvent } from "../components/conversation/streamEventHandler";
import { flushTypewriterBuffer } from "../components/conversation/streamTypewriter";

function makeApplyDeps(opts: {
  items: Item[];
  itemsRef: { current: Item[] };
  typeBufRef: { current: string };
}) {
  const pendingJobIdsRef = { current: [] as string[] };
  const setItems = (updater: Item[] | ((prev: Item[]) => Item[])) => {
    const next = typeof updater === "function" ? updater(opts.items) : updater;
    opts.items = next;
    opts.itemsRef.current = next;
  };
  const appendStreamingText = (chunk: string) => {
    if (!chunk) return;
    setItems((p) => appendStreamingTextToItems(p, chunk));
  };
  const flushTypewriter = () => {
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
    setCompactingStatus: ((_v?: string | null) => {}) as (v: string | null) => void,
    setItems,
    setDistillNotice: () => {},
    setWikiPrepared: () => {},
    setMemoryProposals: () => {},
    setWaitHint: () => {},
    setStatus: () => {},
    setTurnOpen: () => {},
    setPendingJobIds: () => {},
    pendingJobIdsRef,
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
  };
}

function thinkingRows(items: Item[]) {
  return items.filter((i): i is Extract<Item, { kind: "thinking" }> => i.kind === "thinking");
}

describe("upsertStreamingThinking preserves durable id", () => {
  it("stamps an id on the first chunk and keeps it across deltas", () => {
    const once = upsertStreamingThinking([], "First — ");
    const think1 = once.find((i) => i.kind === "thinking") as Extract<
      Item,
      { kind: "thinking" }
    >;
    expect(think1.id).toBeTruthy();
    expect(think1.streaming).toBe(true);

    const twice = upsertStreamingThinking(once, "more tokens");
    const think2 = twice.find((i) => i.kind === "thinking") as Extract<
      Item,
      { kind: "thinking" }
    >;
    expect(think2.id).toBe(think1.id);
    expect(think2.text).toBe("First — more tokens");
  });

  it("strict-appends identical and prefix-looking live deltas", () => {
    // Snapshot coalescing must not run on ordinary upsert — providers can
    // emit repeated or prefix-looking delta:true chunks that are real text.
    let items = upsertStreamingThinking([], "ha");
    items = upsertStreamingThinking(items, "ha");
    expect(thinkingRows(items)[0].text).toBe("haha");

    items = upsertStreamingThinking(items, "h");
    expect(thinkingRows(items)[0].text).toBe("hahah");
  });

  it("keeps the id when streaming ends", () => {
    const live = upsertStreamingThinking([], "reasoning…");
    const id = (live[0] as Extract<Item, { kind: "thinking" }>).id;
    const done = finalizeStreamingThinking(live);
    const think = done[0] as Extract<Item, { kind: "thinking" }>;
    expect(think.streaming).toBeFalsy();
    expect(think.id).toBe(id);
  });

  it("reopens a trailing sealed thinking row instead of one REASONING header per token", () => {
    // Sol/OR word deltas + any mid-stream finalize used to append a new
    // thinking item per chunk (REASONING Muse / REASONING Spark / …).
    let items = upsertStreamingThinking([], "Muse");
    items = finalizeStreamingThinking(items);
    for (const word of [" Spark", " 1", ".", "1"]) {
      items = upsertStreamingThinking(items, word);
      items = finalizeStreamingThinking(items);
    }
    const thinking = items.filter((i) => i.kind === "thinking") as Extract<
      Item,
      { kind: "thinking" }
    >[];
    expect(thinking).toHaveLength(1);
    expect(thinking[0].text).toBe("Muse Spark 1.1");
    expect(thinking[0].id).toBeTruthy();
  });

  it("still starts a new thinking row after a committed assistant bubble", () => {
    let items: Item[] = upsertStreamingThinking([], "phase-one ");
    items = finalizeStreamingThinking(items);
    items = [
      ...items,
      { kind: "msg", msg: { role: "assistant", text: "narration" } },
    ];
    items = upsertStreamingThinking(items, "phase-two");
    const texts = items
      .filter((i): i is Extract<Item, { kind: "thinking" }> => i.kind === "thinking")
      .map((t) => t.text);
    expect(texts).toEqual(["phase-one ", "phase-two"]);
  });

  it("skips trivial sealed assistant crumbs when coalescing word deltas", () => {
    let items: Item[] = upsertStreamingThinking([], "Release");
    items = finalizeStreamingThinking(items);
    items = [
      ...items,
      { kind: "msg", msg: { role: "assistant", text: "**" } },
    ];
    items = upsertStreamingThinking(items, " mechanics");
    items = [
      ...items,
      { kind: "msg", msg: { role: "assistant", text: "****" } },
    ];
    items = upsertStreamingThinking(items, " are");
    const thinking = thinkingRows(items);
    expect(thinking).toHaveLength(1);
    expect(thinking[0].text).toBe("Release mechanics are");
  });

  it("keys thinking by stream_id across sealed assistant surfaces", () => {
    // Identity owns the surface — a flushed finale must not mint one
    // REASONING header per word when stream_id is stable.
    const finalText =
      "Ship it.\n\n"
      + "| Step | Status |\n|---|---|\n"
      + "| CI | green |\n\n"
      + "Ready when you are.";
    let items: Item[] = [
      { kind: "msg", msg: { role: "user", text: "go" } },
      {
        kind: "card",
        card: {
          id: "c1",
          goal: "a.py",
          cwd: null,
          kind: "read_file",
          running: false,
          open: false,
        },
      },
      { kind: "msg", msg: { role: "assistant", text: finalText } },
    ];
    for (const word of ["The", " source", " confirms", " both"]) {
      items = upsertStreamingThinking(items, word, { streamId: "rs_1" });
    }
    const thinking = items.filter((i) => i.kind === "thinking") as Extract<
      Item,
      { kind: "thinking" }
    >[];
    expect(thinking).toHaveLength(1);
    expect(thinking[0].text).toBe("The source confirms both");
    expect(thinking[0].stream_id).toBe("rs_1");
    expect(thinking[0].id).toBeTruthy();
  });
});

describe("createApplyStreamEvent Sol reasoning coalescing", () => {
  it("keeps one durable thinking id/text across word-sized delta:true frames", () => {
    const state = {
      items: [{ kind: "msg", msg: { role: "user", text: "go" } }] as Item[],
      itemsRef: { current: [] as Item[] },
      typeBufRef: { current: "" },
    };
    state.itemsRef.current = state.items;
    const apply = createApplyStreamEvent(makeApplyDeps(state));
    for (const word of ["Release", " mechanics", " are", " now", " verified"]) {
      apply({ kind: "thinking", data: { text: word, delta: true } });
    }
    const thinking = thinkingRows(state.items);
    expect(thinking).toHaveLength(1);
    expect(thinking[0].id).toBeTruthy();
    expect(thinking[0].text).toBe("Release mechanics are now verified");
    expect(thinking[0].streaming).toBe(true);
  });

  it("appends two identical streaming deltas instead of snapshot-deduping them", () => {
    const state = {
      items: [{ kind: "msg", msg: { role: "user", text: "go" } }] as Item[],
      itemsRef: { current: [] as Item[] },
      typeBufRef: { current: "" },
    };
    state.itemsRef.current = state.items;
    const apply = createApplyStreamEvent(makeApplyDeps(state));
    apply({ kind: "thinking", data: { text: "ha", delta: true } });
    apply({ kind: "thinking", data: { text: "ha", delta: true } });
    const thinking = thinkingRows(state.items);
    expect(thinking).toHaveLength(1);
    expect(thinking[0].text).toBe("haha");
    expect(thinking[0].streaming).toBe(true);
  });

  it("coalesces markdown markers split across thinking deltas", () => {
    const state = {
      items: [{ kind: "msg", msg: { role: "user", text: "go" } }] as Item[],
      itemsRef: { current: [] as Item[] },
      typeBufRef: { current: "" },
    };
    state.itemsRef.current = state.items;
    const apply = createApplyStreamEvent(makeApplyDeps(state));
    apply({ kind: "thinking", data: { text: "redesign", delta: true } });
    apply({ kind: "thinking", data: { text: "****", delta: true } });
    apply({ kind: "thinking", data: { text: "Finalizing...", delta: true } });
    const thinking = thinkingRows(state.items);
    expect(thinking).toHaveLength(1);
    expect(thinking[0].text).toBe("redesign****Finalizing...");
  });

  it("ignores interleaved trivial message_delta crumbs between word deltas", () => {
    const state = {
      items: [{ kind: "msg", msg: { role: "user", text: "go" } }] as Item[],
      itemsRef: { current: [] as Item[] },
      typeBufRef: { current: "" },
    };
    state.itemsRef.current = state.items;
    const apply = createApplyStreamEvent(makeApplyDeps(state));
    // With stream_id, reasoning owns its surface — misrouted markdown crumbs
    // on message_delta cannot mint one REASONING row per word.
    apply({
      kind: "thinking",
      data: { text: "Planning ", delta: true, stream_id: "rs_1" },
    });
    apply({ kind: "message_delta", data: { text: "**" } });
    apply({
      kind: "thinking",
      data: { text: "archive ", delta: true, stream_id: "rs_1" },
    });
    apply({ kind: "message_delta", data: { text: "  " } });
    apply({
      kind: "thinking",
      data: { text: "and settle", delta: true, stream_id: "rs_1" },
    });
    const thinking = thinkingRows(state.items);
    expect(thinking).toHaveLength(1);
    expect(thinking[0].text).toBe("Planning archive and settle");
    expect(thinking[0].stream_id).toBe("rs_1");
  });

  it("keeps substantive assistant narration as a hard thinking boundary", () => {
    const state = {
      items: [{ kind: "msg", msg: { role: "user", text: "go" } }] as Item[],
      itemsRef: { current: [] as Item[] },
      typeBufRef: { current: "" },
    };
    state.itemsRef.current = state.items;
    const apply = createApplyStreamEvent(makeApplyDeps(state));
    apply({ kind: "thinking", data: { text: "phase-one ", delta: true } });
    apply({ kind: "message_delta", data: { text: "I will inspect the handler carefully." } });
    apply({ kind: "thinking", data: { text: "phase-two", delta: true } });
    expect(thinkingRows(state.items).map((t) => t.text)).toEqual([
      "phase-one ",
      "phase-two",
    ]);
  });

  it("keeps non-Latin substantive narration as a hard thinking boundary", () => {
    expect(isTrivialAssistantCrumb("調査を続けます。")).toBe(false);
    expect(isTrivialAssistantCrumb("**")).toBe(true);
    expect(isTrivialAssistantCrumb("→")).toBe(false);
    const state = {
      items: [{ kind: "msg", msg: { role: "user", text: "go" } }] as Item[],
      itemsRef: { current: [] as Item[] },
      typeBufRef: { current: "" },
    };
    state.itemsRef.current = state.items;
    const apply = createApplyStreamEvent(makeApplyDeps(state));
    apply({ kind: "thinking", data: { text: "phase-one ", delta: true } });
    apply({ kind: "message_delta", data: { text: "調査を続けます。" } });
    apply({ kind: "thinking", data: { text: "phase-two", delta: true } });
    expect(thinkingRows(state.items).map((t) => t.text)).toEqual([
      "phase-one ",
      "phase-two",
    ]);
    const assistants = state.items.filter(
      (it) => it.kind === "msg" && it.msg.role === "assistant",
    );
    expect(assistants).toHaveLength(1);
    expect(assistants[0]).toMatchObject({
      kind: "msg",
      msg: { role: "assistant", text: "調査を続けます。" },
    });
  });

  it("drops markdown-marker finals so they cannot fence later reasoning", () => {
    const state = {
      items: [{ kind: "msg", msg: { role: "user", text: "go" } }] as Item[],
      itemsRef: { current: [] as Item[] },
      typeBufRef: { current: "" },
    };
    state.itemsRef.current = state.items;
    const apply = createApplyStreamEvent(makeApplyDeps(state));
    apply({ kind: "thinking", data: { text: "Planning ", delta: true } });
    apply({ kind: "message", data: { text: "**" } });
    apply({ kind: "thinking", data: { text: "archive", delta: true } });
    const thinking = thinkingRows(state.items);
    expect(thinking).toHaveLength(1);
    expect(thinking[0].text).toBe("Planning archive");
    const assistants = state.items.filter(
      (it) => it.kind === "msg" && it.msg.role === "assistant",
    );
    expect(assistants).toHaveLength(0);

    // Standalone finalizePilotMessage path (no open streaming bubble).
    const sealed = finalizePilotMessage(
      [{ kind: "msg", msg: { role: "user", text: "go" } }],
      "****",
    );
    expect(sealed.filter((it) => it.kind === "msg" && it.msg.role === "assistant")).toHaveLength(0);

    // Open streaming bubble containing only a markdown marker.
    const fromBubble = finalizePilotMessage(
      [
        { kind: "msg", msg: { role: "user", text: "go" } },
        { kind: "msg", msg: { role: "assistant", text: "**", streaming: true } },
      ],
      undefined,
    );
    expect(
      fromBubble.filter((it) => it.kind === "msg" && it.msg.role === "assistant"),
    ).toHaveLength(0);
  });

  it("opens a new thinking phase after tool_prep/card then coalesces post-tool words", () => {
    const state = {
      items: [{ kind: "msg", msg: { role: "user", text: "go" } }] as Item[],
      itemsRef: { current: [] as Item[] },
      typeBufRef: { current: "" },
    };
    state.itemsRef.current = state.items;
    const apply = createApplyStreamEvent(makeApplyDeps(state));
    apply({ kind: "thinking", data: { text: "analysis-1", delta: true } });
    apply({
      kind: "tool_prep",
      data: { name: "Read", id: "call-1", goal: "foo.ts" },
    });
    for (const word of ["post", " tool", " words"]) {
      apply({ kind: "thinking", data: { text: word, delta: true } });
    }
    const thinking = thinkingRows(state.items);
    expect(thinking).toHaveLength(2);
    expect(thinking[0].text).toBe("analysis-1");
    expect(thinking[1].text).toBe("post tool words");
    expect(thinking[1].id).toBeTruthy();
    expect(thinking[1].id).not.toBe(thinking[0].id);
  });

  it("keeps one stream_id thinking surface without per-token seal/reopen", () => {
    const state = {
      items: [{ kind: "msg", msg: { role: "user", text: "go" } }] as Item[],
      itemsRef: { current: [] as Item[] },
      typeBufRef: { current: "" },
    };
    state.itemsRef.current = state.items;
    const apply = createApplyStreamEvent(makeApplyDeps(state));
    const words = ["Release", " mechanics", " are", " now", " verified"];
    for (const word of words) {
      apply({
        kind: "thinking",
        data: { text: word, delta: true, stream_id: "rs_stable" },
      });
    }
    const thinking = thinkingRows(state.items);
    expect(thinking).toHaveLength(1);
    expect(thinking[0].id).toBeTruthy();
    expect(thinking[0].stream_id).toBe("rs_stable");
    expect(thinking[0].text).toBe("Release mechanics are now verified");
    expect(thinking[0].streaming).toBe(true);
    // Tool-start barrier seals exactly once.
    apply({
      kind: "tool_prep",
      data: { name: "Read", id: "call-1", goal: "foo.ts" },
    });
    const afterTool = thinkingRows(state.items);
    expect(afterTool).toHaveLength(1);
    expect(afterTool[0].streaming).toBeFalsy();
    expect(afterTool[0].id).toBe(thinking[0].id);
  });

  it("interleaves progress + reasoning into one surface each", () => {
    const state = {
      items: [{ kind: "msg", msg: { role: "user", text: "go" } }] as Item[],
      itemsRef: { current: [] as Item[] },
      typeBufRef: { current: "" },
    };
    state.itemsRef.current = state.items;
    const apply = createApplyStreamEvent(makeApplyDeps(state));
    apply({
      kind: "message_delta",
      data: { text: "A1 ", stream_id: "msg_prog", channel: "progress" },
    });
    apply({
      kind: "thinking",
      data: { text: "R1 ", delta: true, stream_id: "rs_1" },
    });
    apply({
      kind: "message_delta",
      data: { text: "A2", stream_id: "msg_prog", channel: "progress" },
    });
    apply({
      kind: "thinking",
      data: { text: "R2", delta: true, stream_id: "rs_1" },
    });
    apply({
      kind: "tool_prep",
      data: { name: "Read", id: "call-x", goal: "x.ts" },
    });
    apply({
      kind: "message_delta",
      data: { text: "F1", stream_id: "msg_final", channel: "answer" },
    });

    const thinking = thinkingRows(state.items);
    expect(thinking).toHaveLength(1);
    expect(thinking[0].text).toBe("R1 R2");
    expect(thinking[0].stream_id).toBe("rs_1");
    // No one-word Thinking rows.
    expect(thinking.every((t) => t.text.trim().split(/\s+/).length >= 2)).toBe(true);

    const progress = state.items.filter(
      (it) =>
        it.kind === "msg"
        && it.msg.role === "assistant"
        && it.msg.stream_id === "msg_prog",
    );
    expect(progress).toHaveLength(1);
    expect(progress[0]).toMatchObject({
      kind: "msg",
      msg: { text: "A1 A2", channel: "progress" },
    });

    const finals = state.items.filter(
      (it) =>
        it.kind === "msg"
        && it.msg.role === "assistant"
        && it.msg.stream_id === "msg_final",
    );
    expect(finals).toHaveLength(1);
    expect(finals[0]).toMatchObject({
      kind: "msg",
      msg: { text: "F1", channel: "answer" },
    });

    const cards = state.items.filter((it) => it.kind === "card");
    expect(cards.length).toBeGreaterThanOrEqual(1);
  });

  it("coalesces non-delta thinking frames through the same upsert path", () => {
    const state = {
      items: [{ kind: "msg", msg: { role: "user", text: "go" } }] as Item[],
      itemsRef: { current: [] as Item[] },
      typeBufRef: { current: "" },
    };
    state.itemsRef.current = state.items;
    const apply = createApplyStreamEvent(makeApplyDeps(state));
    apply({ kind: "thinking", data: { text: "Ring ", delta: false } });
    apply({ kind: "thinking", data: { text: "fragment ", delta: false } });
    apply({ kind: "thinking", data: { text: "replay" } });
    const thinking = thinkingRows(state.items);
    expect(thinking).toHaveLength(1);
    expect(thinking[0].text).toBe("Ring fragment replay");
    expect(thinking[0].streaming).toBeFalsy();
  });

  it("hardens non-delta coalescing against cumulative snapshot frames", () => {
    let items: Item[] = [{ kind: "msg", msg: { role: "user", text: "go" } }];
    items = appendNonStreamingThinking(items, "Hello");
    items = appendNonStreamingThinking(items, "Hello"); // identical snapshot
    items = appendNonStreamingThinking(items, "Hello world"); // strict extension
    items = appendNonStreamingThinking(items, "Hello"); // stale prefix
    const thinking = thinkingRows(items);
    expect(thinking).toHaveLength(1);
    expect(thinking[0].text).toBe("Hello world");

    // True non-overlapping fragments still append.
    items = appendNonStreamingThinking(items, " more");
    expect(thinkingRows(items)[0].text).toBe("Hello world more");
  });

  it("chatFrameToStreamEvent replay matches live word-delta coalescing", () => {
    const live = {
      items: [{ kind: "msg", msg: { role: "user", text: "go" } }] as Item[],
      itemsRef: { current: [] as Item[] },
      typeBufRef: { current: "" },
    };
    live.itemsRef.current = live.items;
    const applyLive = createApplyStreamEvent(makeApplyDeps(live));

    const replay = {
      items: [{ kind: "msg", msg: { role: "user", text: "go" } }] as Item[],
      itemsRef: { current: [] as Item[] },
      typeBufRef: { current: "" },
    };
    replay.itemsRef.current = replay.items;
    const applyReplay = createApplyStreamEvent(makeApplyDeps(replay));

    const frames = [
      { kind: "thinking", data: { text: "Muse", delta: true } },
      { kind: "thinking", data: { text: " Spark", delta: true } },
      { kind: "message_delta", data: { text: "**" } },
      { kind: "thinking", data: { text: " 1.1", delta: true } },
    ];
    for (const frame of frames) {
      applyLive(frame);
      applyReplay(chatFrameToStreamEvent(frame));
    }
    expect(thinkingRows(live.items).map((t) => t.text)).toEqual([
      "Muse Spark 1.1",
    ]);
    expect(thinkingRows(replay.items).map((t) => t.text)).toEqual(
      thinkingRows(live.items).map((t) => t.text),
    );
  });

  it("action_result drops a trivial open pilot crumb so later thinking coalesces", () => {
    // Dual-channel Sol can stream a markdown-marker message_delta while a
    // tool is running; action_result must drop that crumb (not seal it) so
    // post-tool word deltas reopen/coalesce one thinking row.
    const state = {
      items: [{ kind: "msg", msg: { role: "user", text: "go" } }] as Item[],
      itemsRef: { current: [] as Item[] },
      typeBufRef: { current: "" },
    };
    state.itemsRef.current = state.items;
    const apply = createApplyStreamEvent(makeApplyDeps(state));
    apply({
      kind: "action_start",
      data: { id: "a1", kind: "Read", goal: "foo.ts", call_id: "call-1" },
    });
    apply({ kind: "message_delta", data: { text: "**" } });
    apply({
      kind: "action_result",
      data: {
        id: "a1",
        call_id: "call-1",
        kind: "Read",
        goal: "foo.ts",
        status: "complete",
      },
    });
    const assistantsAfterResult = state.items.filter(
      (it) => it.kind === "msg" && it.msg.role === "assistant",
    );
    expect(assistantsAfterResult).toHaveLength(0);

    for (const word of ["post", " tool", " words"]) {
      apply({ kind: "thinking", data: { text: word, delta: true } });
    }
    const thinking = thinkingRows(state.items);
    expect(thinking).toHaveLength(1);
    expect(thinking[0].text).toBe("post tool words");
    expect(thinking[0].id).toBeTruthy();
    expect(
      state.items.filter((it) => it.kind === "msg" && it.msg.role === "assistant"),
    ).toHaveLength(0);
  });
});

describe("activityGroupStableId survives thinking → tool transition", () => {
  it("keeps the same key when a tool card joins a thinking-led group", () => {
    const thinking: Item = {
      kind: "thinking",
      text: "plan",
      streaming: true,
      id: "th-stable-1",
    };
    const before = activityGroupStableId([thinking], 3);
    const withCard = activityGroupStableId(
      [
        thinking,
        {
          kind: "card",
          card: {
            id: "tool-1",
            goal: "read",
            cwd: null,
            kind: "read_file",
            running: true,
            open: false,
          },
        },
      ],
      3,
    );
    expect(withCard).toBe(before);
  });

  it("keeps the same key when thinking arrives after a tool card", () => {
    const card: Item = {
      kind: "card",
      card: {
        id: "tool-2",
        goal: "grep",
        cwd: null,
        kind: "grep",
        running: true,
        open: false,
      },
    };
    const before = activityGroupStableId([card], 1);
    const withThink = activityGroupStableId(
      [
        card,
        { kind: "thinking", text: "hmm", streaming: true, id: "th-late" },
      ],
      1,
    );
    expect(withThink).toBe(before);
  });

  it("does not remount on thinking object identity churn when id is stable", () => {
    const a = activityGroupStableId(
      [{ kind: "thinking", text: "a", streaming: true, id: "th-churn" }],
      0,
    );
    const b = activityGroupStableId(
      [{ kind: "thinking", text: "ab", streaming: true, id: "th-churn" }],
      0,
    );
    expect(b).toBe(a);
  });

  it("keeps the same open-state id when group index shifts after a turn ends", () => {
    const card: Item = {
      kind: "card",
      card: {
        id: "tool-index-shift",
        goal: "read",
        cwd: null,
        kind: "read_file",
        running: false,
        open: false,
      },
    };
    const atThree = activityGroupStableId([card], 3);
    const atFive = activityGroupStableId([card], 5);
    expect(atFive).toBe(atThree);
    expect(atThree).not.toContain("#");
  });
});

describe("liveActivityGroupIndex fences prior turns", () => {
  const sealedCard = (id: string): Extract<Item, { kind: "card" }> => ({
    kind: "card",
    card: {
      id,
      goal: "read foo",
      cwd: null,
      kind: "read_file",
      running: false,
      open: false,
      result: { status: "ok" },
    },
  });

  it("returns -1 when the latest user message has no activity group yet", () => {
    const grouped: GroupedItem[] = [
      { kind: "msg", msg: { role: "user", text: "turn 1" } },
      {
        kind: "activity_group",
        items: [sealedCard("prior-card")],
      },
      { kind: "msg", msg: { role: "assistant", text: "done" } },
      { kind: "msg", msg: { role: "user", text: "turn 2" } },
    ];
    expect(liveActivityGroupIndex(grouped)).toBe(-1);
  });

  it("selects only the activity group after the latest user message", () => {
    const grouped: GroupedItem[] = [
      { kind: "msg", msg: { role: "user", text: "turn 1" } },
      {
        kind: "activity_group",
        items: [{ kind: "thinking", text: "old", id: "th-old" }],
      },
      { kind: "msg", msg: { role: "user", text: "turn 2" } },
      {
        kind: "activity_group",
        items: [{ kind: "thinking", text: "new", id: "th-new" }],
      },
    ];
    expect(liveActivityGroupIndex(grouped)).toBe(3);
  });
});

/** Stable identity keys for append-only order assertions (ignore mutating text). */
function itemOrderKeys(items: Item[]): string[] {
  return items.map((it, i) => {
    if (it.kind === "msg") {
      const sid = it.msg.stream_id || "";
      const ch = it.msg.channel || "";
      // Segment index among same role+stream so post-tool bubbles get a new key.
      let seg = 0;
      for (let j = 0; j < i; j++) {
        const prev = items[j];
        if (
          prev.kind === "msg"
          && prev.msg.role === it.msg.role
          && (prev.msg.stream_id || "") === sid
          && (prev.msg.channel || "") === ch
        ) {
          seg += 1;
        }
      }
      return `msg:${it.msg.role}:${sid || "_"}:${ch || "_"}:${seg}`;
    }
    if (it.kind === "card") return `card:${it.card.id}`;
    if (it.kind === "thinking") return `thinking:${it.id || `anon-${i}`}`;
    if (it.kind === "tool_prep") return `tool_prep:${it.name}`;
    return `${it.kind}:${i}`;
  });
}

function expectOrderIsPrefix(prev: string[], next: string[]) {
  expect(next.slice(0, prev.length)).toEqual(prev);
}

describe("live transcript chronological append-only ordering", () => {
  it("keeps prose → tool → prose → final in event order with stable prefixes", () => {
    const state = {
      items: [{ kind: "msg", msg: { role: "user", text: "go" } }] as Item[],
      itemsRef: { current: [] as Item[] },
      typeBufRef: { current: "" },
    };
    state.itemsRef.current = state.items;
    const apply = createApplyStreamEvent(makeApplyDeps(state));
    const snapshots: string[][] = [];
    const snap = () => {
      const keys = itemOrderKeys(state.items);
      if (snapshots.length > 0) {
        expectOrderIsPrefix(snapshots[snapshots.length - 1], keys);
      }
      snapshots.push(keys);
    };
    snap();

    apply({
      kind: "message_delta",
      data: { text: "block A", stream_id: "S1", channel: "progress" },
    });
    snap();
    expect(state.items.filter((it) => it.kind === "msg" && it.msg.role === "assistant"))
      .toHaveLength(1);

    apply({
      kind: "tool_prep",
      data: { name: "Read", id: "T1", goal: "foo.ts" },
    });
    snap();

    apply({
      kind: "message_delta",
      data: { text: "block B", stream_id: "S1", channel: "progress" },
    });
    snap();

    apply({
      kind: "action_result",
      data: {
        id: "tool-prep:T1",
        call_id: "T1",
        kind: "read_file",
        goal: "foo.ts",
        status: "complete",
      },
    });
    snap();

    apply({
      kind: "message",
      data: { text: "final answer" },
    });
    snap();

    const assistants = state.items.filter(
      (it): it is Extract<Item, { kind: "msg" }> =>
        it.kind === "msg" && it.msg.role === "assistant",
    );
    const cards = state.items.filter((it) => it.kind === "card");
    expect(assistants.map((a) => a.msg.text)).toEqual([
      "block A",
      "block B",
      "final answer",
    ]);
    expect(cards.length).toBeGreaterThanOrEqual(1);

    const kinds = state.items.map((it) => {
      if (it.kind === "msg") {
        return it.msg.role === "user" ? "user" : `bubble:${it.msg.text}`;
      }
      if (it.kind === "card") return `card:${it.card.id || it.card.call_id}`;
      if (it.kind === "tool_prep") return "tool_prep";
      return it.kind;
    });
    // bubble(A), card(T1), bubble(B), final — nothing inserted above earlier rows.
    expect(kinds.indexOf("bubble:block A")).toBeLessThan(
      kinds.findIndex((k) => k.startsWith("card:")),
    );
    expect(kinds.findIndex((k) => k.startsWith("card:"))).toBeLessThan(
      kinds.indexOf("bubble:block B"),
    );
    expect(kinds.indexOf("bubble:block B")).toBeLessThan(
      kinds.indexOf("bubble:final answer"),
    );
  });

  it("opens a new reasoning segment below a tool card for the same stream_id", () => {
    const state = {
      items: [{ kind: "msg", msg: { role: "user", text: "go" } }] as Item[],
      itemsRef: { current: [] as Item[] },
      typeBufRef: { current: "" },
    };
    state.itemsRef.current = state.items;
    const apply = createApplyStreamEvent(makeApplyDeps(state));
    const snapshots: string[][] = [];
    const snap = () => {
      const keys = itemOrderKeys(state.items);
      if (snapshots.length > 0) {
        expectOrderIsPrefix(snapshots[snapshots.length - 1], keys);
      }
      snapshots.push(keys);
    };

    apply({
      kind: "message_delta",
      data: { text: "prose ", stream_id: "S1", channel: "progress" },
    });
    snap();
    apply({
      kind: "thinking",
      data: { text: "R1a ", delta: true, stream_id: "R1" },
    });
    snap();
    apply({
      kind: "tool_prep",
      data: { name: "Read", id: "T1", goal: "x.ts" },
    });
    snap();
    apply({
      kind: "thinking",
      data: { text: "R1b", delta: true, stream_id: "R1" },
    });
    snap();
    apply({
      kind: "message_delta",
      data: { text: "more prose", stream_id: "S1", channel: "progress" },
    });
    snap();

    const thinking = thinkingRows(state.items);
    expect(thinking).toHaveLength(2);
    expect(thinking[0].text).toBe("R1a ");
    expect(thinking[0].stream_id).toBe("R1");
    expect(thinking[0].streaming).toBeFalsy();
    expect(thinking[1].text).toBe("R1b");
    expect(thinking[1].stream_id).toBe("R1");
    expect(thinking[1].id).not.toBe(thinking[0].id);

    const kinds = state.items.map((it) => {
      if (it.kind === "msg" && it.msg.role === "assistant") {
        return `bubble:${it.msg.text}`;
      }
      if (it.kind === "thinking") return `thinking:${it.text}`;
      if (it.kind === "card") return "card";
      if (it.kind === "tool_prep") return "tool_prep";
      if (it.kind === "msg") return "user";
      return it.kind;
    });
    // Reasoning continuation is BELOW the tool card; post-tool prose is a new bubble.
    expect(kinds).toEqual([
      "user",
      "bubble:prose ",
      "thinking:R1a ",
      "card",
      "tool_prep",
      "thinking:R1b",
      "bubble:more prose",
    ]);
  });
});
