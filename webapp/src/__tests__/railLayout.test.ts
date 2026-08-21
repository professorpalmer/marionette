import { describe, expect, it } from "vitest";
import {
  LEFT_MIN_W,
  MIN_CENTER_W,
  RIGHT_COMPACT_MIN_W,
  RIGHT_MIN_W,
  layoutChrome,
  reclampRailWidths,
} from "../lib/railLayout";

describe("reclampRailWidths", () => {
  it("keeps the left rail at its current width when the right board opens", () => {
    const innerWidth = 1100;
    const leftW = 248;
    const rightW = 520;
    const next = reclampRailWidths(leftW, rightW, true, true, innerWidth);
    expect(next.leftW).toBe(leftW);
    expect(next.rightW).toBeLessThanOrEqual(rightW);
    expect(next.leftW + next.rightW + MIN_CENTER_W + layoutChrome(true, true))
      .toBeLessThanOrEqual(innerWidth);
  });

  it("does not shrink a fitting left rail on a wide window", () => {
    const next = reclampRailWidths(320, 520, true, true, 1600);
    expect(next.leftW).toBe(320);
    expect(next.rightW).toBe(520);
  });

  it("compacts the right board before the left rail on a tight window", () => {
    const next = reclampRailWidths(248, 520, true, true, 900);
    expect(next.leftW).toBeGreaterThanOrEqual(LEFT_MIN_W);
    expect(next.rightW).toBeLessThan(520);
    expect(next.rightW).toBeGreaterThanOrEqual(RIGHT_COMPACT_MIN_W);
    expect(next.rightW).toBeLessThanOrEqual(RIGHT_MIN_W);
  });
});
