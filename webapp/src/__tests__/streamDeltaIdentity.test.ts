import { expect, it } from "vitest";
import type { Item } from "../components/TranscriptList";
import { ensureAssistantStreamingBubble } from "../components/conversation/streamApply";
import { finalizeStreamingThinking } from "../components/conversation/thinkingToolPrep";

// A plain message_delta onto an already-open bubble changes nothing structural;
// returning the same array lets React skip a full transcript re-render per token.
it("a delta onto an open bubble keeps the items array identity", () => {
  const items: Item[] = [
    { kind: "msg", msg: { role: "user", text: "hi" } },
    { kind: "msg", msg: { role: "assistant", text: "Hel", streaming: true } },
  ];
  expect(ensureAssistantStreamingBubble(items, { chunk: "lo" })).toBe(items);
  expect(finalizeStreamingThinking(items)).toBe(items);
});

it("still seals a streaming thinking row", () => {
  const items: Item[] = [
    { kind: "thinking", text: "why", streaming: true, id: "t1", started_at_ms: 1 } as Item,
  ];
  const sealed = finalizeStreamingThinking(items);
  expect(sealed).not.toBe(items);
  expect((sealed[0] as { streaming?: boolean }).streaming).toBeUndefined();
});
