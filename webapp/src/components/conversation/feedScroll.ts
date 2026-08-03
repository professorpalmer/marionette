/**
 * Stick-to-bottom / session-switch settle helpers for the transcript feed.
 */

export const FEED_PIN_THRESHOLD_PX = 120;
/** Inner live-reasoning pane re-pin threshold (smaller than outer feed). */
export const THINKING_INNER_PIN_THRESHOLD_PX = 48;
export const FEED_SETTLE_STABLE_FRAMES = 5;
export const FEED_SETTLE_MAX_FRAMES = 90;
/** Hard wall-clock cap so settle glue cannot outlive stream height churn. */
export const FEED_SETTLE_TIMEOUT_MS = 1000;
/** Bubbles from nested live-reasoning panes when the user reads away from the tail. */
export const FEED_UNPIN_BUBBLE_EVENT = "pmharness-feed-unpin";

export function isPinnedToBottom(
  scrollHeight: number,
  scrollTop: number,
  clientHeight: number,
  thresholdPx: number = FEED_PIN_THRESHOLD_PX,
): boolean {
  return scrollHeight - scrollTop - clientHeight < thresholdPx;
}

/**
 * Pin state from live scroll geometry. Settling must never force-true — the
 * [items] effect keeps glue via scrollSettlingRef separately.
 */
export function pinStateFromScrollGeometry(
  scrollHeight: number,
  scrollTop: number,
  clientHeight: number,
  _settling: boolean,
  thresholdPx: number = FEED_PIN_THRESHOLD_PX,
): boolean {
  void _settling;
  return isPinnedToBottom(scrollHeight, scrollTop, clientHeight, thresholdPx);
}

/** Upward wheel should unpin (unless settle glue is active). */
export function shouldUnpinOnWheel(deltaY: number, settling: boolean): boolean {
  if (settling) return false;
  return deltaY < 0;
}

/**
 * Nested panes (live ThinkingBlock) stop wheel bubble at scroll edges so the
 * outer feed does not steal deltas — the feed must listen in capture phase so
 * upward gestures still unpin before stopPropagation runs.
 */
export function shouldStopNestedWheelBubble(
  deltaY: number,
  atTop: boolean,
  atBottom: boolean,
): boolean {
  return (deltaY < 0 && !atTop) || (deltaY > 0 && !atBottom);
}

/** Live inner reasoning stops tail-follow on upward wheel. */
export function shouldUnpinInnerOnWheel(deltaY: number): boolean {
  return deltaY < 0;
}

/** Passive capture options for feed wheel unpin (runs before nested handlers). */
export function feedWheelUnpinListenerOptions(): AddEventListenerOptions {
  return { passive: true, capture: true };
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
  /** Wall-clock start of the settle loop (performance.now() or Date). */
  startedAtMs?: number;
  /** Current time paired with startedAtMs. */
  nowMs?: number;
  timeoutMs?: number;
}): { stableFrames: number; frame: number; done: boolean } {
  const stableFrames =
    opts.height === opts.lastHeight ? opts.stableFrames + 1 : 0;
  const frame = opts.frame + 1;
  const timeoutMs = opts.timeoutMs ?? FEED_SETTLE_TIMEOUT_MS;
  const timedOut =
    opts.startedAtMs != null &&
    opts.nowMs != null &&
    opts.nowMs - opts.startedAtMs >= timeoutMs;
  const done =
    timedOut ||
    stableFrames >= FEED_SETTLE_STABLE_FRAMES ||
    frame > FEED_SETTLE_MAX_FRAMES;
  return { stableFrames, frame, done };
}
