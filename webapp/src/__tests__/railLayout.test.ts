import { describe, expect, it } from "vitest";
import {
  MIN_CENTER_W,
  RIGHT_DOCK_INSET_PX,
  chatSurfaceWidth,
  layoutChrome,
  overlayRightBoardWidth,
  reclampRailWidths,
  rightDockInsetPx,
} from "../lib/railLayout";

describe("reclampRailWidths", () => {
  it("keeps the left rail and chat surface when the right board opens", () => {
    const innerWidth = 1100;
    const leftW = 248;
    const rightW = 520;
    const closed = reclampRailWidths(leftW, rightW, true, false, innerWidth);
    const next = reclampRailWidths(leftW, rightW, true, true, innerWidth);
    expect(next.leftW).toBe(leftW);
    expect(next.rightW).toBe(rightW);
    expect(chatSurfaceWidth(innerWidth, next.leftW, true))
      .toBe(chatSurfaceWidth(innerWidth, closed.leftW, true));
    expect(next.leftW + MIN_CENTER_W + layoutChrome(true))
      .toBeLessThanOrEqual(innerWidth);
  });

  it("does not shrink a fitting left rail on a wide window", () => {
    const next = reclampRailWidths(320, 520, true, true, 1600);
    expect(next.leftW).toBe(320);
    expect(next.rightW).toBe(520);
  });

  it("compacts the overlay only when it cannot fit on the chat surface", () => {
    const next = reclampRailWidths(248, 520, true, true, 656);
    expect(next.leftW).toBe(248);
    expect(next.rightW).toBeLessThan(520);
    expect(next.rightW).toBe(chatSurfaceWidth(656, next.leftW, true));
  });
});

it.each([360, 640, 1024])("keeps the overlay on the chat surface at %ipx", width => {
  const result = reclampRailWidths(248, 520, true, true, width);
  expect(result.leftW).toBeGreaterThan(0);
  expect(result.rightW).toBeGreaterThan(0);
  expect(result.leftW + layoutChrome(true)).toBeLessThanOrEqual(width);
  expect(result.rightW).toBeLessThanOrEqual(
    chatSurfaceWidth(width, result.leftW, true),
  );
});

it("sits the floating dock on the chat edge, then just left of an open board", () => {
  expect(rightDockInsetPx(false, 520)).toBe(RIGHT_DOCK_INSET_PX);
  expect(rightDockInsetPx(true, 520)).toBe(520 + 6 + RIGHT_DOCK_INSET_PX);
  expect(overlayRightBoardWidth(520, 400)).toBe(400);
});
