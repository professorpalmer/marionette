import { describe, expect, it } from "vitest";
import {
  GRID_COLUMN_COUNT,
  applyPairwiseColumnResize,
  normalizeGroupWidths,
  showColumnResizeHandle,
} from "../lib/boardColumnWidths";

describe("showColumnResizeHandle", () => {
  it("hides handles when there is only one column", () => {
    expect(showColumnResizeHandle(0, 1)).toBe(false);
  });

  it("shows a handle on every column except the leftmost", () => {
    expect(showColumnResizeHandle(0, 3)).toBe(true);
    expect(showColumnResizeHandle(1, 3)).toBe(true);
    expect(showColumnResizeHandle(2, 3)).toBe(false);
  });
});

describe("applyPairwiseColumnResize", () => {
  it("resizes a pair of columns without touching a third", () => {
    expect(applyPairwiseColumnResize([4, 4, 4], 1, 6)).toEqual([4, 6, 2]);
  });

  it("keeps two-column complementary math", () => {
    expect(applyPairwiseColumnResize([6, 6], 0, 7)).toEqual([7, 5]);
  });

  it("clamps to the pair minimum so a middle column cannot vanish", () => {
    expect(applyPairwiseColumnResize([4, 4, 4], 1, 20)).toEqual([4, 6, 2]);
  });

  it("leaves the leftmost column unchanged when asked to resize it", () => {
    expect(applyPairwiseColumnResize([4, 4, 4], 2, 8)).toEqual([4, 4, 4]);
  });
});

describe("normalizeGroupWidths", () => {
  it("fills a 12-column board evenly", () => {
    const widths = normalizeGroupWidths([4, 4, 4], 0);
    expect(widths.reduce((sum, width) => sum + width, 0)).toBe(GRID_COLUMN_COUNT);
    expect(widths).toEqual([4, 4, 4]);
  });
});
