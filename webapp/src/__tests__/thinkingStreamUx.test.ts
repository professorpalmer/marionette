import { describe, expect, it } from "vitest";
import {
  finalizeStreamingThinking,
  upsertStreamingThinking,
} from "../components/Conversation";
import { activityGroupStableId } from "../components/TranscriptList";
import type { Item } from "../components/TranscriptList";

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

  it("keeps the id when streaming ends", () => {
    const live = upsertStreamingThinking([], "reasoning…");
    const id = (live[0] as Extract<Item, { kind: "thinking" }>).id;
    const done = finalizeStreamingThinking(live);
    const think = done[0] as Extract<Item, { kind: "thinking" }>;
    expect(think.streaming).toBeFalsy();
    expect(think.id).toBe(id);
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
