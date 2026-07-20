import { describe, expect, it } from "vitest";
import { hostHasLayout, safePtyDims } from "../components/terminalDims";

describe("safePtyDims", () => {
  it("passes through positive sizes", () => {
    expect(safePtyDims(120, 40)).toEqual({ cols: 120, rows: 40 });
  });

  it("falls back when FitAddon reports 0x0", () => {
    expect(safePtyDims(0, 0)).toEqual({ cols: 80, rows: 24 });
  });

  it("fills only the missing axis", () => {
    expect(safePtyDims(100, 0)).toEqual({ cols: 100, rows: 24 });
    expect(safePtyDims(0, 12)).toEqual({ cols: 80, rows: 12 });
  });

  it("floors fractional dims and rejects NaN", () => {
    expect(safePtyDims(80.9, 24.1)).toEqual({ cols: 80, rows: 24 });
    expect(safePtyDims(Number.NaN, Number.NaN)).toEqual({ cols: 80, rows: 24 });
  });
});

describe("hostHasLayout", () => {
  it("is false for null or zero-size hosts", () => {
    expect(hostHasLayout(null)).toBe(false);
    expect(hostHasLayout({ clientWidth: 0, clientHeight: 40 } as HTMLElement)).toBe(false);
  });

  it("is true when both axes have size", () => {
    expect(hostHasLayout({ clientWidth: 10, clientHeight: 10 } as HTMLElement)).toBe(true);
  });
});
