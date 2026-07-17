import { describe, expect, it } from "vitest";
import {
  appendActionStartCard,
  appendStreamingTextToItems,
  ensureAssistantStreamingBubble,
  finalizeOpenPilotBubble,
  finalizePilotMessage,
  sealOpenStreamSurfaces,
  upsertStreamingThinking,
  upsertToolPrep,
} from "../components/Conversation";
import {
  collectIntermediateAssistantItems,
  type Item,
} from "../components/TranscriptList";

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
});
