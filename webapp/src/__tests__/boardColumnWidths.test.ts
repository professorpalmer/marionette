import { describe, expect, it } from "vitest";
import {
  GRID_COLUMN_COUNT,
  absorbShellResize,
  applyPairwiseColumnResize,
  columnSpanFromPointerDelta,
  columnTrackTemplate,
  groupGridColumn,
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

describe("groupGridColumn", () => {
  it("maps right-indexed groups onto left-to-right tracks", () => {
    expect(groupGridColumn(0, 1)).toBe("1");
    expect(groupGridColumn(0, 2)).toBe("2");
    expect(groupGridColumn(1, 2)).toBe("1");
    expect(groupGridColumn(0, 3)).toBe("3");
    expect(groupGridColumn(2, 3)).toBe("1");
  });
});

describe("columnTrackTemplate", () => {
  it("uses a single flexible track for one column", () => {
    expect(columnTrackTemplate([12])).toBe("minmax(0, 1fr)");
  });

  it("weights unmeasured columns so the leftmost is first", () => {
    expect(columnTrackTemplate([7, 5])).toBe("minmax(0, 5fr) minmax(0, 7fr)");
  });

  it("locks non-leftmost columns to pixels once the board is measured", () => {
    expect(columnTrackTemplate([7, 5], 1200)).toBe("minmax(0, 1fr) 700px");
  });
});

describe("columnSpanFromPointerDelta", () => {
  it("moves continuously instead of snapping to whole grid columns", () => {
    expect(columnSpanFromPointerDelta({
      startSpan: 6,
      startClientX: 100,
      clientX: 90,
      boardWidth: 1200,
    })).toBeCloseTo(6.1);
  });
});

describe("absorbShellResize", () => {
  it("gives extra board width to the leftmost column only", () => {
    const next = absorbShellResize([6, 6], 1200, 1400);
    expect(next[0]).toBeCloseTo((600 / 1400) * GRID_COLUMN_COUNT);
    expect(next[1]).toBeCloseTo((800 / 1400) * GRID_COLUMN_COUNT);
    expect(next[0] + next[1]).toBeCloseTo(GRID_COLUMN_COUNT);
  });
});

describe("applyPairwiseColumnResize", () => {
  it("resizes a pair of columns without touching a third", () => {
    expect(applyPairwiseColumnResize([4, 4, 4], 1, 6)).toEqual([4, 6, 2]);
  });

  it("keeps two-column complementary math", () => {
    expect(applyPairwiseColumnResize([6, 6], 0, 7)).toEqual([7, 5]);
  });

  it("allows fractional spans so pointer drags do not grid-snap", () => {
    const next = applyPairwiseColumnResize([6, 6], 0, 6.4);
    expect(next[0]).toBeCloseTo(6.4);
    expect(next[1]).toBeCloseTo(5.6);
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
