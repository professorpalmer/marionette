import { describe, expect, it } from "vitest";
import {
  clampStackSplit,
  DEFAULT_STACK_SPLIT,
  MAX_STACK_SPLIT,
  MIN_STACK_SPLIT,
  stackPairKey,
  stackRowTemplate,
  stackSplitFromDrag,
  stackSplitFromKey,
} from "../lib/stackSplit";

describe("stackSplit", () => {
  it("clamps to a usable top/bottom range", () => {
    expect(clampStackSplit(Number.NaN)).toBe(DEFAULT_STACK_SPLIT);
    expect(clampStackSplit(0)).toBe(MIN_STACK_SPLIT);
    expect(clampStackSplit(1)).toBe(MAX_STACK_SPLIT);
    expect(clampStackSplit(0.28)).toBe(0.28);
  });

  it("keys a stack by visual order so a swap is a different pair", () => {
    expect(stackPairKey(["terminal", "swarm"])).toBe("terminal|swarm");
    expect(stackPairKey(["swarm", "terminal"])).toBe("swarm|terminal");
  });

  it("emits fr rows so a small top card leaves a tall bottom card", () => {
    expect(stackRowTemplate(0.5)).toBe("minmax(0, 50fr) minmax(0, 50fr)");
    expect(stackRowTemplate(0.2)).toBe("minmax(0, 20fr) minmax(0, 80fr)");
    expect(stackRowTemplate(0.05)).toBe(`minmax(0, ${MIN_STACK_SPLIT * 100}fr) minmax(0, ${(1 - MIN_STACK_SPLIT) * 100}fr)`);
  });

  it("maps pointer delta against stack height", () => {
    expect(
      stackSplitFromDrag({
        startSplit: 0.5,
        startClientY: 100,
        clientY: 80,
        stackHeight: 400,
      }),
    ).toBe(0.45);
    expect(
      stackSplitFromDrag({
        startSplit: 0.5,
        startClientY: 100,
        clientY: 100,
        stackHeight: 0,
      }),
    ).toBe(0.5);
  });

  it("nudges the split from arrow keys", () => {
    expect(stackSplitFromKey(0.5, "ArrowUp")).toBe(0.45);
    expect(stackSplitFromKey(0.5, "ArrowDown")).toBe(0.55);
    expect(stackSplitFromKey(0.5, "ArrowLeft")).toBe(0.5);
  });
});
