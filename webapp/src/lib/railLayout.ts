/** Shell rail widths: keep the left rail put and compact the right board first. */

export const MIN_CENTER_W = 360;
export const LEFT_MIN_W = 180;
export const LEFT_MAX_W = 420;
export const RIGHT_MIN_W = 320;
export const RIGHT_COMPACT_MIN_W = 220;

/** Flex chrome around the center column: shell padding and rail gutters. */
export const RAIL_GUTTER_W = 6;

const clamp = (n: number, lo: number, hi: number) => Math.max(lo, Math.min(hi, n));

export function layoutChrome(leftOpen: boolean, rightOpen: boolean): number {
  let gutters = 0;
  if (leftOpen) gutters += 1;
  if (rightOpen) gutters += 1;
  return 2 + gutters * RAIL_GUTTER_W;
}

/**
 * Keep open rails within min/window budget while preserving MIN_CENTER_W.
 * Opening the right board must not steal width from a left rail that still fits.
 * If the window cannot hold both preferred widths plus the chat column, compact
 * the right board first so the left rail stays at its current size.
 */
export function reclampRailWidths(
  leftW: number,
  rightW: number,
  leftOpen: boolean,
  rightOpen: boolean,
  innerWidth: number,
): { leftW: number; rightW: number } {
  const chrome = layoutChrome(leftOpen, rightOpen);
  const availableWidth = Math.max(0, innerWidth - chrome);
  const preferredLeft = leftOpen ? clamp(leftW, LEFT_MIN_W, LEFT_MAX_W) : 0;
  const preferredRight = rightOpen ? Math.max(RIGHT_MIN_W, rightW) : 0;
  const requiredRails = (leftOpen ? LEFT_MIN_W : 0) + (rightOpen ? RIGHT_MIN_W : 0);
  const centerWidth = Math.min(MIN_CENTER_W, Math.max(0, availableWidth - requiredRails));
  const railBudget = Math.max(0, availableWidth - centerWidth);

  if (!leftOpen && !rightOpen) return { leftW, rightW };

  if (leftOpen && rightOpen) {
    const compactRightMin = Math.min(RIGHT_MIN_W, RIGHT_COMPACT_MIN_W, railBudget);
    const compactLeftMin = Math.min(LEFT_MIN_W, Math.max(0, railBudget - compactRightMin));
    const leftMax = Math.min(LEFT_MAX_W, Math.max(compactLeftMin, railBudget - compactRightMin));
    const left = clamp(preferredLeft, compactLeftMin, leftMax);
    const right = clamp(
      preferredRight,
      compactRightMin,
      Math.max(compactRightMin, railBudget - left),
    );
    return { leftW: left, rightW: right };
  }

  if (leftOpen) {
    const leftMin = Math.min(LEFT_MIN_W, railBudget);
    return {
      leftW: clamp(preferredLeft, leftMin, Math.min(LEFT_MAX_W, railBudget)),
      rightW,
    };
  }

  const rightMin = Math.min(RIGHT_MIN_W, railBudget);
  return {
    leftW,
    rightW: clamp(preferredRight, rightMin, railBudget),
  };
}
