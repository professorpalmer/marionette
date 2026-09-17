/** Shell rail widths: left rail consumes space; the right board overlays chat. */

export const MIN_CENTER_W = 360;
export const LEFT_MIN_W = 180;
export const LEFT_MAX_W = 420;
export const RIGHT_MIN_W = 320;
export const RIGHT_COMPACT_MIN_W = 220;

/** Flex chrome around the center column: shell padding and the left-rail gutter. */
export const RAIL_GUTTER_W = 6;
/** Floating dock gap from the chat-surface edge (Tailwind `right-4`). */
export const RIGHT_DOCK_INSET_PX = 16;

const clamp = (n: number, lo: number, hi: number) => Math.max(lo, Math.min(hi, n));

export function layoutChrome(leftOpen: boolean, rightOpen = false): number {
  // Right board overlays the chat surface and does not take a flex gutter.
  void rightOpen;
  return 2 + (leftOpen ? RAIL_GUTTER_W : 0);
}

export function chatSurfaceWidth(
  innerWidth: number,
  leftW: number,
  leftOpen: boolean,
): number {
  const left = leftOpen ? leftW : 0;
  return Math.max(0, innerWidth - layoutChrome(leftOpen) - left);
}

export function overlayRightBoardWidth(rightW: number, overlayBudget: number): number {
  if (overlayBudget <= 0) return 0;
  const preferred = Math.max(RIGHT_MIN_W, rightW);
  const minW = Math.min(RIGHT_COMPACT_MIN_W, overlayBudget);
  return clamp(preferred, minW, overlayBudget);
}

export function rightDockInsetPx(rightOpen: boolean, rightW: number): number {
  if (!rightOpen) return RIGHT_DOCK_INSET_PX;
  return rightW + RAIL_GUTTER_W + RIGHT_DOCK_INSET_PX;
}

/**
 * Keep the left rail within min/window budget. The right board floats over
 * the chat surface, so opening it must not shrink the centered transcript.
 */
export function reclampRailWidths(
  leftW: number,
  rightW: number,
  leftOpen: boolean,
  rightOpen: boolean,
  innerWidth: number,
): { leftW: number; rightW: number } {
  const chrome = layoutChrome(leftOpen);
  const availableWidth = Math.max(0, innerWidth - chrome);

  if (!leftOpen && !rightOpen) return { leftW, rightW };

  if (leftOpen) {
    const leftMin = Math.min(LEFT_MIN_W, availableWidth);
    const left = clamp(leftW, leftMin, Math.min(LEFT_MAX_W, availableWidth));
    const overlayBudget = Math.max(0, availableWidth - left);
    return {
      leftW: left,
      rightW: rightOpen ? overlayRightBoardWidth(rightW, overlayBudget) : rightW,
    };
  }

  return {
    leftW,
    rightW: rightOpen ? overlayRightBoardWidth(rightW, availableWidth) : rightW,
  };
}
