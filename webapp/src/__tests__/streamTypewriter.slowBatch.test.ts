import { describe, expect, it } from "vitest";
import { pumpTypewriterFrame } from "../components/conversation/streamTypewriter";

function refs(buf: string) {
  return {
    typeBufRef: { current: buf },
    typeRafRef: { current: null as number | null },
    typeDoneRef: { current: false },
  };
}

describe("streamTypewriter slow-model batching", () => {
  it("paints a large slow-model burst in one frame instead of dripping", () => {
    const burst = "token ".repeat(40);
    const r = refs(burst);
    const painted: string[] = [];
    pumpTypewriterFrame(r, (chunk) => painted.push(chunk), (cb) => {
      return 1;
    });
    expect(painted.join("")).toBe(burst);
    expect(painted).toHaveLength(1);
    expect(r.typeBufRef.current).toBe("");
  });
});
