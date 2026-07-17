/**
 * Stick-to-bottom / session-switch settle helpers for the transcript feed.
 */

export const FEED_PIN_THRESHOLD_PX = 120;
export const FEED_SETTLE_STABLE_FRAMES = 5;
export const FEED_SETTLE_MAX_FRAMES = 90;

export function isPinnedToBottom(
  scrollHeight: number,
  scrollTop: number,
  clientHeight: number,
  thresholdPx: number = FEED_PIN_THRESHOLD_PX,
): boolean {
  return scrollHeight - scrollTop - clientHeight < thresholdPx;
}

/** Upward wheel should unpin (unless settle glue is active). */
export function shouldUnpinOnWheel(deltaY: number, settling: boolean): boolean {
  if (settling) return false;
  return deltaY < 0;
}

/** Touch drag downward (finger moves down → content scrolls up) unpins. */
export function shouldUnpinOnTouchMove(
  startY: number | null,
  currentY: number | null,
  settling: boolean,
): boolean {
  if (settling || startY == null || currentY == null) return false;
  return currentY > startY + 2;
}

export function settleFrameResult(opts: {
  height: number;
  lastHeight: number;
  stableFrames: number;
  frame: number;
}): { stableFrames: number; frame: number; done: boolean } {
  const stableFrames =
    opts.height === opts.lastHeight ? opts.stableFrames + 1 : 0;
  const frame = opts.frame + 1;
  const done =
    stableFrames >= FEED_SETTLE_STABLE_FRAMES || frame > FEED_SETTLE_MAX_FRAMES;
  return { stableFrames, frame, done };
}
