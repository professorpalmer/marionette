import { afterEach, describe, expect, it, vi } from "vitest";
import { createChatEventsReattach } from "../components/conversation/chatEventsReattach";
import { recordPrimaryStreamFrame } from "../components/conversation/chatEvents";
import { transcriptResponseToItems } from "../components/conversation/transcriptItems";
import { api } from "../lib/api";
import type { Item } from "../components/TranscriptList";

// Ring cursors restart at every stream generation (one per turn), so two turns
// of the same length carry the same cursors. Store ids keep counting.
function turnFrames(generation: number, text: string, firstStoreId: number) {
  const frames = [
    { kind: "message_delta", data: { text, channel: "answer" } },
    { kind: "message", data: { role: "assistant", text, streamed: true } },
    { kind: "assistant_done", data: { stop_cause: "natural" } },
  ];
  return frames.map((frame, i) => ({
    id: firstStoreId + i,
    kind: "stream",
    data: { ...frame, cursor: i + 1, generation },
    session_id: "sess",
  }));
}

function reattach(opts: {
  since: number;
  ringCursor: number;
  ringGeneration: number | undefined;
  applied: { kind: string; text: string }[];
  items?: Item[];
}) {
  const refs = {
    lastAppliedCursorRef: { current: opts.since },
    lastAppliedRingCursorRef: { current: opts.ringCursor },
    ringGenerationRef: { current: opts.ringGeneration },
  };
  let items: Item[] = opts.items ?? [];
  const itemsRef = { current: items };
  const { pullChatEvents } = createChatEventsReattach({
    cancelled: () => false,
    loadGen: 1,
    transcriptLoadGenRef: { current: 1 },
    streamGenRef: { current: 1 },
    reattachGen: 1,
    reattachSid: "sess",
    cachedSessionIdRef: { current: "sess" },
    localStreamActiveRef: { current: false },
    userStoppedRef: { current: false },
    ...refs,
    detachedBusyRef: { current: false },
    runnerBusyPollGenRef: { current: 0 },
    itemsRef,
    transcriptFpRef: { current: "" },
    chatEventsPollTimerRef: { current: null },
    chatEventsLiveCancelRef: { current: null },
    applyStreamEventRef: {
      current: (ev) => { opts.applied.push({ kind: ev.kind, text: String(ev.data?.text ?? "") }); },
    },
    flushTypewriterRef: { current: () => {} },
    maybeRunQueuedResumeRef: { current: () => {} },
    maybeDrainQueueRef: { current: () => {} },
    clearChatEventsPoll: () => {},
    setItems: (next) => {
      items = typeof next === "function" ? next(items) : next;
      itemsRef.current = items;
    },
    setTranscriptStale: () => {},
    setTurnOpen: () => {},
    setStatus: () => {},
  });
  return { pullChatEvents, refs };
}

describe("session switch store replay", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("a replay from the start applies only the newest turn over the hydrated transcript", async () => {
    // Switching back reset the store cursor to 0. Before, turn 1's final
    // message replayed onto turn 2's bubble and turn 2's frames were dropped
    // as duplicates of turn 1's cursors: turn 2's answer vanished.
    const events = [...turnFrames(1, "turn one answer", 1), ...turnFrames(2, "turn two answer", 4)];
    vi.spyOn(api, "readEventsSince").mockResolvedValue({
      ok: true, session_id: "sess", cursor: 6, events,
    } as any);
    const applied: { kind: string; text: string }[] = [];
    const { pullChatEvents, refs } = reattach({ since: 0, ringCursor: 0, ringGeneration: undefined, applied });

    await pullChatEvents();

    expect(applied.map((a) => a.text).filter(Boolean)).toEqual(["turn two answer", "turn two answer"]);
    expect(applied.some((a) => a.text.includes("turn one"))).toBe(false);
    expect(refs.ringGenerationRef.current).toBe(2);
    expect(refs.lastAppliedRingCursorRef.current).toBe(3);
  });

  it("a later turn's low cursors are not duplicates of an earlier turn", async () => {
    // The live watch left (generation 1, cursor 145). A turn the client did not
    // start restarts its cursors at 1; every frame of it must still apply.
    vi.spyOn(api, "readEventsSince").mockResolvedValue({
      ok: true, session_id: "sess", cursor: 303, events: turnFrames(2, "next turn", 301),
    } as any);
    const applied: { kind: string; text: string }[] = [];
    const { pullChatEvents, refs } = reattach({ since: 300, ringCursor: 145, ringGeneration: 1, applied });

    await pullChatEvents();

    expect(applied.map((a) => a.kind)).toEqual(["message_delta", "message", "assistant_done"]);
    expect(refs.ringGenerationRef.current).toBe(2);
    expect(refs.lastAppliedRingCursorRef.current).toBe(3);
  });

  it("an older turn's frame does not apply once a newer turn is applied", async () => {
    vi.spyOn(api, "readEventsSince").mockResolvedValue({
      ok: true, session_id: "sess", cursor: 11, events: turnFrames(1, "stale", 9),
    } as any);
    const applied: { kind: string; text: string }[] = [];
    const { pullChatEvents } = reattach({ since: 8, ringCursor: 2, ringGeneration: 2, applied });

    await pullChatEvents();

    expect(applied).toEqual([]);
  });
});

describe("ring position", () => {
  it("moves to a newer generation with that generation's cursor", () => {
    const lastAppliedRingCursorRef = { current: 145 };
    const ringGenerationRef: { current: number | undefined } = { current: 1 };
    recordPrimaryStreamFrame({ lastAppliedRingCursorRef, ringGenerationRef }, { cursor: 3, generation: 2 });
    expect([ringGenerationRef.current, lastAppliedRingCursorRef.current]).toEqual([2, 3]);
  });

  it("ignores a frame from an older generation", () => {
    const lastAppliedRingCursorRef = { current: 3 };
    const ringGenerationRef: { current: number | undefined } = { current: 2 };
    recordPrimaryStreamFrame({ lastAppliedRingCursorRef, ringGenerationRef }, { cursor: 140, generation: 1 });
    expect([ringGenerationRef.current, lastAppliedRingCursorRef.current]).toEqual([2, 3]);
  });
});

describe("Thought rows across a hydrate", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("hydrate keeps a display Thought row with its duration", () => {
    const items = transcriptResponseToItems({ display: [
      { type: "message", role: "user", text: "go" },
      { type: "thinking", text: "Read the handler first.", duration_ms: 1200 },
      { type: "message", role: "assistant", text: "Done." },
    ] });
    expect(items.map((it) => it.kind)).toEqual(["msg", "thinking", "msg"]);
    expect(items[1]).toMatchObject({ kind: "thinking", text: "Read the handler first.", duration_ms: 1200 });
    expect((items[1] as { streaming?: boolean }).streaming).toBeFalsy();
  });

  it("a replay paints one Thought chunk per reasoning run", async () => {
    // Per-word frames cannot prove a replay of a hydrated Thought. One joined
    // chunk equals the Thought text, so the handler can skip it.
    const frame = (id: number, kind: string, data: Record<string, unknown>) => ({
      id, kind: "stream", session_id: "sess", data: { kind, data, cursor: id, generation: 1 },
    });
    vi.spyOn(api, "readEventsSince").mockResolvedValue({ ok: true, session_id: "sess", cursor: 5, events: [
      frame(1, "thinking", { text: "Read the ", delta: true, channel: "reasoning" }),
      frame(2, "thinking", { text: "handler first.", delta: true, channel: "reasoning" }),
      frame(3, "message_delta", { text: "Done.", channel: "answer" }),
      frame(4, "thinking", { text: "Then check.", delta: true, channel: "reasoning" }),
      frame(5, "assistant_done", {}),
    ] } as any);
    const applied: { kind: string; text: string }[] = [];
    const { pullChatEvents, refs } = reattach({ since: 0, ringCursor: 0, ringGeneration: undefined, applied });

    await pullChatEvents();

    expect(applied).toEqual([
      { kind: "thinking", text: "Read the handler first." },
      { kind: "message_delta", text: "Done." },
      { kind: "thinking", text: "Then check." },
      { kind: "assistant_done", text: "" },
    ]);
    expect(refs.lastAppliedRingCursorRef.current).toBe(5);
  });

  it("a replay from the start skips Thoughts the hydrated turn holds, once each", async () => {
    const thought = (id: number, text: string) => ({
      id, kind: "stream", session_id: "sess",
      data: { kind: "thinking", data: { text, delta: true, channel: "reasoning" }, cursor: id, generation: 1 },
    });
    const done = (id: number) => ({
      id, kind: "stream", session_id: "sess",
      data: { kind: "stream_item_done", data: {}, cursor: id, generation: 1 },
    });
    vi.spyOn(api, "readEventsSince").mockResolvedValue({ ok: true, session_id: "sess", cursor: 7, events: [
      thought(1, "Read the "), thought(2, "handler first."), done(3),
      thought(4, "Read the handler first."), done(5),
      thought(6, "Read the handler first."), done(7),
    ] } as any);
    const hydrated: Item[] = [
      { kind: "msg", msg: { role: "user", text: "go" } },
      { kind: "thinking", text: "Read the handler first." },
      { kind: "card", card: { id: "c1", kind: "run_command", goal: "ls" } as any },
      { kind: "thinking", text: "Read the handler first." },
    ];
    const applied: { kind: string; text: string }[] = [];
    const { pullChatEvents } = reattach({ since: 0, ringCursor: 0, ringGeneration: undefined, applied, items: hydrated });

    await pullChatEvents();

    // Two held Thoughts cover two replayed chunks; the third is new.
    expect(applied.filter((a) => a.kind === "thinking")).toEqual([
      { kind: "thinking", text: "Read the handler first." },
    ]);
  });

  it("a later poll paints a repeated Thought", async () => {
    vi.spyOn(api, "readEventsSince").mockResolvedValue({ ok: true, session_id: "sess", cursor: 11, events: [{
      id: 11, kind: "stream", session_id: "sess",
      data: { kind: "thinking", data: { text: "Read the handler first.", delta: true }, cursor: 9, generation: 1 },
    }] } as any);
    const applied: { kind: string; text: string }[] = [];
    const { pullChatEvents } = reattach({
      since: 10, ringCursor: 8, ringGeneration: 1, applied,
      items: [
        { kind: "msg", msg: { role: "user", text: "go" } },
        { kind: "thinking", text: "Read the handler first." },
      ],
    });

    await pullChatEvents();

    expect(applied).toEqual([{ kind: "thinking", text: "Read the handler first." }]);
  });
});
