/**
 * Stick-to-bottom / session-switch settle helpers for the transcript feed.
 */

import { isOccludedScrollParentSize } from "./transcriptVirtualWindow";

export const FEED_PIN_THRESHOLD_PX = 120;
/**
 * Re-attach stick-to-bottom only when the viewport is this close to the true
 * end. Kept tight so a light Mac trackpad nudge cannot "still count as pinned"
 * and fight streaming growth.
 */
export const FEED_REPIN_THRESHOLD_PX = 28;
/** After the last wheel/touch/user-scroll event, wait this long before re-pin. */
export const FEED_GESTURE_IDLE_MS = 150;
/** Treat the viewport as at the true tail (not merely the 28px re-pin band). */
export const FEED_TAIL_EPSILON_PX = 0.5;
/** Inner live-reasoning pane re-pin threshold (smaller than outer feed). */
export const THINKING_INNER_PIN_THRESHOLD_PX = 48;
export const FEED_SETTLE_STABLE_FRAMES = 5;
export const FEED_SETTLE_MAX_FRAMES = 90;
/** Hard wall-clock cap so settle glue cannot outlive stream height churn. */
export const FEED_SETTLE_TIMEOUT_MS = 1000;
/** Bubbles from nested live-reasoning panes when the user reads away from the tail. */
export const FEED_UNPIN_BUBBLE_EVENT = "pmharness-feed-unpin";

/**
 * Stick-to-bottom follow flush policy.
 *
 * ResizeObserver runs after layout and before paint. Applying scrollTop
 * there keeps streaming tokens (and chrome-driven clientHeight shrink)
 * in the same frame. requestAnimationFrame runs after paint, so deferring
 * follow paints one frame of growth / composer-stack shrink then snaps —
 * the stream-at-bottom viewport lurch.
 */
export function chooseFeedFollowFlush(): "before_paint" {
  return "before_paint";
}

/** Feed scrollport overflow-anchor — auto; pin hysteresis owns unstick (never "none"). */
export const FEED_SCROLLPORT_OVERFLOW_ANCHOR = "auto" as const;

/** CSS custom property on the chat column; drives scroll-padding-bottom on the feed scrollport. */
export const FEED_CHROME_CLEARANCE_VAR = "--feed-chrome-clearance";

/** Bottom spacer so the last stream line sits above the composer/status stack. */
export function feedBottomClearancePx(chromeHeight: number): number {
  if (!Number.isFinite(chromeHeight) || chromeHeight <= 0) return 96;
  return Math.max(72, Math.min(Math.round(chromeHeight), 480));
}

/** Authoritative scrollTop for stick-to-bottom (not scrollToIndex align:end). */
export function scrollToFeedEnd(scrollHeight: number, clientHeight: number): number {
  return Math.max(0, scrollHeight - clientHeight);
}

export function isPinnedToBottom(
  scrollHeight: number,
  scrollTop: number,
  clientHeight: number,
  thresholdPx: number = FEED_PIN_THRESHOLD_PX,
): boolean {
  return scrollHeight - scrollTop - clientHeight < thresholdPx;
}

/**
 * Pin state from live scroll geometry. Settling glue is tracked separately via
 * scrollSettlingRef and honored by scrollTopAfterFeedHeightChange.
 *
 * Prefer {@link nextFeedPinState} for the live feed: geometry alone re-pins
 * inside a large threshold and fights trackpad unpin + streaming stick.
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

/**
 * Next stick-to-bottom state with gesture hysteresis.
 *
 * Light Mac trackpad scrolls fire wheel-up (unpin) then a scroll event that is
 * still within the old 120px "near bottom" band. Without a release latch,
 * onScroll re-pins and the next stream token yanks the feed back — stutter.
 *
 * Rules:
 * - Upward wheel/touch sets ``releasedByGesture``; stay unpinned until the user
 *   scrolls toward the bottom AND lands within ``repinPx`` of the end.
 * - A still-active user gesture does not re-pin merely by entering the 28px
 *   band (mid-flick). Hitting the true tail (distance ~ 0) latches pin.
 * - ``wasPinned && !releasedByGesture`` survives content growth that pushes
 *   distance past the re-pin band — follow still owns the new max.
 * - Without a gesture release, pin follows the tight re-pin threshold only.
 */
export function shouldDeferFollowDuringUserGesture(
  userGestureActive: boolean,
): boolean {
  return userGestureActive;
}

export function isAtFeedTail(
  scrollHeight: number,
  scrollTop: number,
  clientHeight: number,
  epsilonPx: number = FEED_TAIL_EPSILON_PX,
): boolean {
  const maxScrollTop = Math.max(0, scrollHeight - clientHeight);
  return Math.abs(scrollTop - maxScrollTop) < epsilonPx;
}

export function nextFeedPinState(opts: {
  wasPinned: boolean;
  releasedByGesture: boolean;
  scrollHeight: number;
  scrollTop: number;
  clientHeight: number;
  /** Prior scrollTop; null on first observation. */
  prevScrollTop: number | null;
  settling: boolean;
  repinPx?: number;
  /** Wheel/touch/scrollbar/keyboard gesture still in flight. */
  userGestureActive?: boolean;
}): { pinned: boolean; releasedByGesture: boolean } {
  const repinPx = opts.repinPx ?? FEED_REPIN_THRESHOLD_PX;
  const userGestureActive = opts.userGestureActive ?? false;
  const distance =
    opts.scrollHeight - opts.scrollTop - opts.clientHeight;
  const nearBottom = distance < repinPx;
  const atTail = isAtFeedTail(
    opts.scrollHeight,
    opts.scrollTop,
    opts.clientHeight,
  );
  const scrolledTowardBottom =
    opts.prevScrollTop != null && opts.scrollTop > opts.prevScrollTop + 0.5;
  const scrolledAway =
    opts.prevScrollTop != null && opts.scrollTop < opts.prevScrollTop - 0.5;

  if (opts.settling) {
    return { pinned: true, releasedByGesture: false };
  }

  if (opts.releasedByGesture) {
    // Mid-flick above the tail: do not latch just because we entered 28px.
    if (userGestureActive && !atTail) {
      return { pinned: false, releasedByGesture: true };
    }
    if (atTail || (scrolledTowardBottom && nearBottom)) {
      return { pinned: true, releasedByGesture: false };
    }
    return { pinned: false, releasedByGesture: true };
  }

  // Keep stick-to-bottom across token growth. Height can jump so the old
  // max sits well outside the 28px band before follow writes the new max.
  if (opts.wasPinned) {
    if (scrolledAway && !nearBottom) {
      return { pinned: false, releasedByGesture: false };
    }
    return { pinned: true, releasedByGesture: false };
  }

  if (nearBottom && !userGestureActive) {
    return { pinned: true, releasedByGesture: false };
  }
  if (atTail) {
    return { pinned: true, releasedByGesture: false };
  }
  return { pinned: false, releasedByGesture: false };
}

/** Show the jump-to-latest control only while the user has read away. */
export function shouldShowJumpToBottom(opts: {
  pinned: boolean;
  settling: boolean;
}): boolean {
  return !opts.pinned && !opts.settling;
}

/** Upward wheel unpins even during session-switch settle glue. */
export function shouldUnpinOnWheel(deltaY: number, _settling: boolean): boolean {
  void _settling;
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
  _settling: boolean,
): boolean {
  void _settling;
  if (startY == null || currentY == null) return false;
  return currentY > startY + 2;
}

/**
 * After a feed/content height change, derive the scrollTop Conversation should
 * apply. Returns null when the viewport must stay put (released/unpinned growth).
 *
 * Gesture release wins over session-switch settling so manual scroll-up during
 * settle glue is not overwritten by ResizeObserver follow.
 */
export function scrollTopAfterFeedHeightChange(opts: {
  scrollHeight: number;
  scrollTop: number;
  clientHeight: number;
  pinned: boolean;
  settling: boolean;
  releasedByGesture: boolean;
  userGestureActive?: boolean;
}): number | null {
  if (opts.releasedByGesture) {
    return null;
  }
  // Mid-flick above the tail: do not steal scrollTop. Once pinned (they
  // latched the end), keep following growth even if the downward gesture
  // has not idled yet — otherwise new tokens walk out from under them.
  if (
    shouldDeferFollowDuringUserGesture(opts.userGestureActive ?? false) &&
    !opts.pinned &&
    !opts.settling
  ) {
    return null;
  }
  if (!opts.pinned && !opts.settling) {
    return null;
  }
  const maxScrollTop = Math.max(0, opts.scrollHeight - opts.clientHeight);
  if (Math.abs(opts.scrollTop - maxScrollTop) < 0.5) {
    return null;
  }
  return maxScrollTop;
}

export type FeedResizeFollowResult =
  | { kind: "noop" }
  | { kind: "follow"; scrollTop: number }
  | { kind: "restore_pin_only" };

/** Ignore sub-pixel scroll noise when comparing observation vs rAF geometry. */
export const FEED_RESIZE_SCROLL_MOVEMENT_EPSILON_PX = 0.5;

export type FeedResizeObservationSnapshot = {
  pinned: boolean;
  settling: boolean;
  scrollTop: number;
  scrollHeight: number;
};

/**
 * Coalesce ResizeObserver callbacks scheduled into one rAF: pin/settling
 * ownership merges monotonically, while scroll geometry stays at the earliest
 * observation so keyboard/scrollbar scroll-up between callbacks is detectable.
 */
export function mergeFeedResizeObservationSnapshots(
  existing: FeedResizeObservationSnapshot | null,
  incoming: FeedResizeObservationSnapshot,
): FeedResizeObservationSnapshot {
  if (!existing) return incoming;
  return {
    pinned: existing.pinned || incoming.pinned,
    settling: existing.settling || incoming.settling,
    scrollTop: existing.scrollTop,
    scrollHeight: existing.scrollHeight,
  };
}

/**
 * Manual scroll-away (keyboard, scrollbar, programmatic) between observation
 * and rAF: scrollTop drops while content height is stable or grew. Content
 * shrink clamp (both scrollHeight and scrollTop fall) is not manual release.
 */
export function shouldCancelFeedResizeFollowForManualScrollAway(opts: {
  snapshotScrollTop: number;
  snapshotScrollHeight: number;
  liveScrollTop: number;
  liveScrollHeight: number;
  epsilonPx?: number;
}): boolean {
  const epsilon = opts.epsilonPx ?? FEED_RESIZE_SCROLL_MOVEMENT_EPSILON_PX;
  const scrollTopDecreased =
    opts.liveScrollTop < opts.snapshotScrollTop - epsilon;
  const contentDidNotShrink =
    opts.liveScrollHeight >= opts.snapshotScrollHeight - epsilon;
  return scrollTopDecreased && contentDidNotShrink;
}

/**
 * ResizeObserver rAF apply: honor pin ownership captured at observation time,
 * skip occluded 0×0 parents, and let gesture release cancel follow.
 */
export function feedResizeScrollFollowDecision(opts: {
  scrollHeight: number;
  scrollTop: number;
  clientHeight: number;
  offsetHeight: number;
  snapshotPinned: boolean;
  snapshotSettling: boolean;
  snapshotScrollTop: number;
  snapshotScrollHeight: number;
  releasedByGesture: boolean;
  userGestureActive?: boolean;
}): FeedResizeFollowResult {
  if (isOccludedScrollParentSize(opts.clientHeight, opts.offsetHeight)) {
    return { kind: "noop" };
  }
  if (opts.releasedByGesture) {
    return { kind: "noop" };
  }
  if (
    shouldDeferFollowDuringUserGesture(opts.userGestureActive ?? false) &&
    !opts.snapshotPinned &&
    !opts.snapshotSettling
  ) {
    return { kind: "noop" };
  }
  if (
    shouldCancelFeedResizeFollowForManualScrollAway({
      snapshotScrollTop: opts.snapshotScrollTop,
      snapshotScrollHeight: opts.snapshotScrollHeight,
      liveScrollTop: opts.scrollTop,
      liveScrollHeight: opts.scrollHeight,
    })
  ) {
    return { kind: "noop" };
  }
  if (!opts.snapshotPinned && !opts.snapshotSettling) {
    return { kind: "noop" };
  }
  const top = scrollTopAfterFeedHeightChange({
    scrollHeight: opts.scrollHeight,
    scrollTop: opts.scrollTop,
    clientHeight: opts.clientHeight,
    pinned: opts.snapshotPinned,
    settling: opts.snapshotSettling,
    releasedByGesture: false,
  });
  if (top != null) {
    return { kind: "follow", scrollTop: top };
  }
  return { kind: "restore_pin_only" };
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
