import { describe, expect, it } from "vitest";
import type { GroupedItem } from "../components/TranscriptList";
import { partitionTranscriptLiveTail } from "../components/conversation/transcriptLiveTail";

describe("partitionTranscriptLiveTail", () => {
  const streamingMsg: GroupedItem = {
    kind: "msg",
    msg: { role: "assistant", text: "live", streaming: true },
  };
  const sealedMsg: GroupedItem = {
    kind: "msg",
    msg: { role: "user", text: "hi" },
  };

  it("keeps streaming tail rows out of the virtual window while the loop is open", () => {
    const grouped: GroupedItem[] = [sealedMsg, streamingMsg];
    const part = partitionTranscriptLiveTail(grouped, {
      lastLiveActivityIdx: -1,
      agentLoopOpen: true,
    });
    expect(part.head).toEqual([sealedMsg]);
    expect(part.tail).toEqual([streamingMsg]);
    expect(part.tailStartIndex).toBe(1);
  });

  it("virtualizes the full list when the turn is closed", () => {
    const grouped: GroupedItem[] = [sealedMsg, streamingMsg];
    const part = partitionTranscriptLiveTail(grouped, {
      lastLiveActivityIdx: -1,
      agentLoopOpen: false,
    });
    expect(part.head).toEqual(grouped);
    expect(part.tail).toEqual([]);
  });
});
