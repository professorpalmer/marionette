import { describe, expect, it } from "vitest";
import { runProgrammaticScroll } from "../components/conversation/feedScroll";

describe("programmatic scroll mark", () => {
  it("clears at once when the write moves nothing (no scroll event will come)", () => {
    const el = { scrollTop: 900 };
    const mark = { current: false };
    runProgrammaticScroll(el, mark, () => { el.scrollTop = 900; });
    expect(mark.current).toBe(false);
  });

  it("stays set for a write that moves, until its scroll event arrives", () => {
    const el = { scrollTop: 400 };
    const mark = { current: false };
    runProgrammaticScroll(el, mark, () => { el.scrollTop = 900; });
    expect(mark.current).toBe(true);
  });
});
