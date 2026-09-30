/**
 * Stick-to-bottom / session-switch settle helpers for the transcript feed.
 */

import { isOccludedScrollParentSize } from "./transcriptVirtualWindow";

export const FEED_PIN_THRESHOLD_PX = 120;
/**
 * Re-attach stick-to-bottom after an input interrupt. ~70px is wide enough to
 * catch a flick that lands near the end without treating a light trackpad
 * nudge (still well above this band) as pinned. Unpin is input-based, not
 * "distance exceeded this number".
 */
export const FEED_REPIN_THRESHOLD_PX = 70;
/** After the last wheel/touch/keyboard/user-scroll event, wait this long before re-pin. */
export const FEED_GESTURE_IDLE_MS = 150;
/** Treat the viewport as at the true tail (not merely the re-pin band). */
export const FEED_TAIL_EPSILON_PX = 0.5;
/** Discrete spring: close residual error after feed-forwarding content growth. */
export const FEED_FOLLOW_STIFFNESS = 0.42;
export const FEED_FOLLOW_DAMPING = 0.78;
export const FEED_FOLLOW_SETTLE_PX = 0.5;
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

/**
 * Composer clearance (Cursor-like). Always keep this gap between the last
 * transcript paint and the composer dock — idle and live. The locked
 * scrollport pair remains overflow-anchor + scroll-padding-bottom.
 *
 * 0.9.445 zeroed content pad while streaming to reduce pin/anchor fight;
 * that glued Investigating/status chrome to the composer. Stick-to-bottom
 * still uses scrollTop=max including this pad (same as Cursor).
 */
export const FEED_COMPOSER_CLEARANCE_PX = 64;
export const FEED_SCROLLPORT_SCROLL_PADDING_BOTTOM_PX = FEED_COMPOSER_CLEARANCE_PX;
export const FEED_CONTENT_PADDING_BOTTOM_PX = FEED_COMPOSER_CLEARANCE_PX;

/** Tokens landing, or the turn latch still open (pin follow path). */
export function feedLiveStreamOpen(
  status: string,
  turnOpen = false,
): boolean {
  return turnOpen || status === "streaming";
}

/**
 * Content padding-bottom — always the composer clearance. ``liveStreamOpen``
 * is retained for callers/tests but no longer collapses the Cursor gap.
 */
export function feedSeatingReservePx(opts: {
  liveStreamOpen: boolean;
}): number {
  void opts.liveStreamOpen;
  return FEED_COMPOSER_CLEARANCE_PX;
}

/** Scrollport style: overflow-anchor + scroll-padding-bottom only. */
export function feedScrollportStyle(): {
  overflowAnchor: typeof FEED_SCROLLPORT_OVERFLOW_ANCHOR;
  scrollPaddingBottom: number;
} {
  return {
    overflowAnchor: FEED_SCROLLPORT_OVERFLOW_ANCHOR,
    scrollPaddingBottom: FEED_SCROLLPORT_SCROLL_PADDING_BOTTOM_PX,
  };
}

/** Feed inner column: fill the scrollport, pack from the top, never flex-end. */
export function feedContentLayoutClass(): string {
  return "max-w-3xl mx-auto px-6 pt-6 min-h-full flex flex-col justify-start gap-1";
}

/** Authoritative scrollTop for stick-to-bottom (not scrollToIndex align:end). */
export function scrollToFeedEnd(scrollHeight: number, clientHeight: number): number {
  return Math.max(0, scrollHeight - clientHeight);
}

/** Submit always re-pins. Resize follow will not pick this up if the pin was stale. */
export function applyUserSubmitFeedPin(opts: {
  scrollHeight: number;
  clientHeight: number;
}): { pinned: true; settling: true; scrollTop: number } {
  return {
    pinned: true,
    settling: true,
    scrollTop: scrollToFeedEnd(opts.scrollHeight, opts.clientHeight),
  };
}

/** First submit paint always pins. A later paint honors a post-submit release. */
export function submitPinShouldAbort(opts: {
  pass: "first" | "later";
  releasedSinceSubmit: boolean;
}): boolean {
  return opts.pass === "later" && opts.releasedSinceSubmit;
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
 * - Input interrupt (wheel/touch/keyboard) sets ``releasedByGesture``; stay
 *   unpinned until the user scrolls toward the bottom AND lands within
 *   ``repinPx`` of the end.
 * - A still-active user gesture does not re-pin merely by entering the restick
 *   band (mid-flick). Hitting the true tail (distance ~ 0) latches pin.
 * - ``wasPinned && !releasedByGesture`` survives content growth that pushes
 *   distance past the re-pin band — follow still owns the new max.
 * - Geometry-only scrollTop changes (overflow-anchor, compositor) do not
 *   unpin; ``inputInterrupt`` is required.
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
  /** Explicit input (wheel/touch/keyboard/scrollbar), not geometry alone. */
  inputInterrupt?: boolean;
}): { pinned: boolean; releasedByGesture: boolean } {
  const repinPx = opts.repinPx ?? FEED_REPIN_THRESHOLD_PX;
  const userGestureActive = opts.userGestureActive ?? false;
  const inputInterrupt = opts.inputInterrupt ?? userGestureActive;
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
    // Mid-flick above the tail: do not latch just because we entered the band.
    if (userGestureActive && !atTail) {
      return { pinned: false, releasedByGesture: true };
    }
    if (atTail || (scrolledTowardBottom && nearBottom)) {
      return { pinned: true, releasedByGesture: false };
    }
    return { pinned: false, releasedByGesture: true };
  }

  // Keep stick-to-bottom across token growth. Height can jump so the old
  // max sits well outside the restick band before follow writes the new max.
  // Unpin only on input — overflow-anchor / compositor scrollTop noise must
  // not release the pin.
  if (opts.wasPinned) {
    if (inputInterrupt && scrolledAway && !nearBottom) {
      return { pinned: false, releasedByGesture: true };
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
  atTail?: boolean;
  distanceFromEndPx?: number;
}): boolean {
  if (opts.pinned || opts.settling || opts.atTail) return false;
  if (
    opts.distanceFromEndPx != null
    && opts.distanceFromEndPx < FEED_REPIN_THRESHOLD_PX
  ) {
    return false;
  }
  return true;
}

/** Upward wheel unpins even during session-switch settle glue. */
export function shouldUnpinOnWheel(deltaY: number, _settling: boolean): boolean {
  void _settling;
  return deltaY < 0;
}

const FEED_UNPIN_KEYS = new Set(["PageUp", "ArrowUp", "Home"]);

/** Keyboard interrupt — PageUp / ArrowUp / Home. Composer is outside the feed. */
export function shouldUnpinOnKeyboard(key: string): boolean {
  return FEED_UNPIN_KEYS.has(key);
}

/** Wheel, touch, keyboard — not a scrollbar-position inference. */
export function isFeedInputInterrupt(
  source: "wheel" | "touch" | "keyboard" | "scrollbar",
): boolean {
  return (
    source === "wheel"
    || source === "touch"
    || source === "keyboard"
    || source === "scrollbar"
  );
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
export type FeedSpringFollowState = {
  scrollTop: number;
  velocityPxPerMs: number;
};

/**
 * Stick-to-bottom follow: absorb content growth in the same frame (feed-
 * forward), then spring any residual error toward max. A bare
 * ``scrollTop = max`` snap fights overflow-anchor and hitchs when chrome
 * shrinks a frame behind layout.
 */
export function nextFeedSpringFollow(opts: {
  scrollTop: number;
  maxScrollTop: number;
  contentDeltaPx: number;
  velocityPxPerMs: number;
  dtMs?: number;
  stiffness?: number;
  damping?: number;
}): FeedSpringFollowState {
  const stiffness = opts.stiffness ?? FEED_FOLLOW_STIFFNESS;
  const damping = opts.damping ?? FEED_FOLLOW_DAMPING;
  void opts.dtMs;
  // Feed-forward content growth so a pinned tail stays a pinned tail in the
  // same frame. Spring only the leftover error (chrome / subpixel).
  const forwarded = feedForwardFollowTop(
    opts.scrollTop,
    opts.contentDeltaPx,
    opts.maxScrollTop,
  );
  const error = opts.maxScrollTop - forwarded;
  if (Math.abs(error) < FEED_FOLLOW_SETTLE_PX) {
    return { scrollTop: forwarded, velocityPxPerMs: 0 };
  }
  const velocity = (opts.velocityPxPerMs + error * stiffness) * damping;
  const next = forwarded + velocity;
  const clamped = Math.max(0, Math.min(opts.maxScrollTop, next));
  if (Math.abs(opts.maxScrollTop - clamped) < FEED_FOLLOW_SETTLE_PX) {
    return { scrollTop: opts.maxScrollTop, velocityPxPerMs: 0 };
  }
  return { scrollTop: clamped, velocityPxPerMs: velocity };
}

/** Same-frame feed-forward: keep distance-from-end across content growth. */
export function feedForwardFollowTop(
  scrollTop: number,
  contentDeltaPx: number,
  maxScrollTop: number,
): number {
  const forwarded = scrollTop + Math.max(0, contentDeltaPx);
  return Math.max(0, Math.min(maxScrollTop, forwarded));
}

export function scrollTopAfterFeedHeightChange(opts: {
  scrollHeight: number;
  scrollTop: number;
  clientHeight: number;
  pinned: boolean;
  settling: boolean;
  releasedByGesture: boolean;
  userGestureActive?: boolean;
  /** Prior scrollHeight so follow can feed-forward the growth delta. */
  prevScrollHeight?: number;
  velocityPxPerMs?: number;
  dtMs?: number;
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
  if (opts.prevScrollHeight == null) {
    if (Math.abs(opts.scrollTop - maxScrollTop) < FEED_FOLLOW_SETTLE_PX) {
      return null;
    }
    return maxScrollTop;
  }
  const contentDeltaPx = opts.scrollHeight - opts.prevScrollHeight;
  if (contentDeltaPx <= 0) {
    if (Math.abs(opts.scrollTop - maxScrollTop) < FEED_FOLLOW_SETTLE_PX) {
      return null;
    }
    return maxScrollTop;
  }
  const sprung = nextFeedSpringFollow({
    scrollTop: opts.scrollTop,
    maxScrollTop,
    contentDeltaPx,
    velocityPxPerMs: opts.velocityPxPerMs ?? 0,
    dtMs: opts.dtMs ?? 16,
  });
  if (Math.abs(opts.scrollTop - sprung.scrollTop) < FEED_FOLLOW_SETTLE_PX) {
    return null;
  }
  return sprung.scrollTop;
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
    prevScrollHeight: opts.snapshotScrollHeight,
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

/**
 * Run a scroll write marked as ours, so the scroll handler does not read it
 * as a user gesture. A write that moves nothing fires no scroll event, so the
 * mark is dropped at once; left set, it swallowed the user's next real scroll.
 */
export function runProgrammaticScroll(
  el: { scrollTop: number } | null,
  mark: { current: boolean },
  write: () => void,
): void {
  const before = el?.scrollTop ?? 0;
  mark.current = true;
  write();
  if (!el || Math.abs(el.scrollTop - before) < 0.5) mark.current = false;
}
