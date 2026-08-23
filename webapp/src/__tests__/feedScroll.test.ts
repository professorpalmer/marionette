import { describe, expect, it } from "vitest";
import {
  FEED_GESTURE_IDLE_MS,
  FEED_REPIN_THRESHOLD_PX,
  chooseFeedFollowFlush,
  feedBottomClearancePx,
  feedResizeScrollFollowDecision,
  mergeFeedResizeObservationSnapshots,
  nextFeedPinState,
  scrollTopAfterFeedHeightChange,
  settleFrameResult,
  shouldCancelFeedResizeFollowForManualScrollAway,
  shouldDeferFollowDuringUserGesture,
  shouldShowJumpToBottom,
  shouldUnpinOnTouchMove,
  shouldUnpinOnWheel,
} from "../components/conversation/feedScroll";

describe("feedScroll hysteresis", () => {
  const height = 2000;
  const client = 400;

  it("does not re-pin a light upward nudge still inside the old near-bottom band", () => {
    const lightUpTop = height - client - 40;
    expect(
      nextFeedPinState({
        wasPinned: true,
        releasedByGesture: true,
        scrollHeight: height,
        scrollTop: lightUpTop,
        clientHeight: client,
        prevScrollTop: lightUpTop + 8,
        settling: false,
        repinPx: FEED_REPIN_THRESHOLD_PX,
      }),
    ).toEqual({ pinned: false, releasedByGesture: true });
  });

  it("re-pins only after scrolling toward the tight bottom band", () => {
    const lightUpTop = height - client - 40;
    expect(
      nextFeedPinState({
        wasPinned: false,
        releasedByGesture: true,
        scrollHeight: height,
        scrollTop: height - client - 10,
        clientHeight: client,
        prevScrollTop: lightUpTop,
        settling: false,
        repinPx: FEED_REPIN_THRESHOLD_PX,
      }),
    ).toEqual({ pinned: true, releasedByGesture: false });
  });

  it("upward wheel and touch unpins even during session-switch settle glue", () => {
    expect(shouldUnpinOnWheel(-12, false)).toBe(true);
    expect(shouldUnpinOnWheel(-12, true)).toBe(true);
    expect(shouldUnpinOnTouchMove(10, 20, true)).toBe(true);
    const settle = settleFrameResult({
      height: 2000,
      lastHeight: 2000,
      stableFrames: 4,
      frame: 4,
    });
    expect(settle.done).toBe(true);
  });

  it("shows jump-to-latest only when unpinned and not settling", () => {
    expect(shouldShowJumpToBottom({ pinned: true, settling: false })).toBe(false);
    expect(shouldShowJumpToBottom({ pinned: false, settling: true })).toBe(false);
    expect(shouldShowJumpToBottom({ pinned: false, settling: false })).toBe(true);
  });
});

describe("scrollTopAfterFeedHeightChange", () => {
  const client = 400;

  it("follows pinned expansion to the new max", () => {
    const oldMax = 1600 - client;
    expect(
      scrollTopAfterFeedHeightChange({
        scrollHeight: 2000,
        scrollTop: oldMax,
        clientHeight: client,
        pinned: true,
        settling: false,
        releasedByGesture: false,
      }),
    ).toBe(2000 - client);
  });

  it("follows pinned expansion when growth leaves old scrollTop outside repin threshold", () => {
    const oldMax = 1600 - client;
    const newHeight = 2200;
    const distanceFromBottom = newHeight - oldMax - client;
    expect(distanceFromBottom).toBeGreaterThan(FEED_REPIN_THRESHOLD_PX);
    // Growth must keep pin so ResizeObserver follow can advance to the new max.
    expect(
      nextFeedPinState({
        wasPinned: true,
        releasedByGesture: false,
        scrollHeight: newHeight,
        scrollTop: oldMax,
        clientHeight: client,
        prevScrollTop: oldMax,
        settling: false,
        repinPx: FEED_REPIN_THRESHOLD_PX,
      }),
    ).toEqual({ pinned: true, releasedByGesture: false });
    expect(
      scrollTopAfterFeedHeightChange({
        scrollHeight: newHeight,
        scrollTop: oldMax,
        clientHeight: client,
        pinned: true,
        settling: false,
        releasedByGesture: false,
      }),
    ).toBe(newHeight - client);
  });

  it("follows pinned collapse to the new max", () => {
    expect(
      scrollTopAfterFeedHeightChange({
        scrollHeight: 1200,
        scrollTop: 1600,
        clientHeight: client,
        pinned: true,
        settling: false,
        releasedByGesture: false,
      }),
    ).toBe(800);
  });

  it("leaves scrollTop unchanged when released and content grows", () => {
    expect(
      scrollTopAfterFeedHeightChange({
        scrollHeight: 2200,
        scrollTop: 400,
        clientHeight: client,
        pinned: false,
        settling: false,
        releasedByGesture: true,
      }),
    ).toBeNull();
  });

  it("does not follow fold or tool-shelf expansion while unpinned", () => {
    expect(
      scrollTopAfterFeedHeightChange({
        scrollHeight: 2400,
        scrollTop: 200,
        clientHeight: client,
        pinned: false,
        settling: false,
        releasedByGesture: false,
      }),
    ).toBeNull();
    expect(
      feedResizeScrollFollowDecision({
        scrollHeight: 2400,
        scrollTop: 200,
        clientHeight: client,
        offsetHeight: client,
        snapshotPinned: false,
        snapshotSettling: false,
        snapshotScrollTop: 200,
        snapshotScrollHeight: 2000,
        releasedByGesture: false,
      }),
    ).toEqual({ kind: "noop" });
  });

  it("follows during settling when the user has not released", () => {
    expect(
      scrollTopAfterFeedHeightChange({
        scrollHeight: 1800,
        scrollTop: 1000,
        clientHeight: client,
        pinned: true,
        settling: true,
        releasedByGesture: false,
      }),
    ).toBe(1400);
  });

  it("gesture release wins over settling glue", () => {
    expect(
      scrollTopAfterFeedHeightChange({
        scrollHeight: 1800,
        scrollTop: 400,
        clientHeight: client,
        pinned: true,
        settling: true,
        releasedByGesture: true,
      }),
    ).toBeNull();
  });

  it("returns null when already at the max", () => {
    const max = 2000 - client;
    expect(
      scrollTopAfterFeedHeightChange({
        scrollHeight: 2000,
        scrollTop: max,
        clientHeight: client,
        pinned: true,
        settling: false,
        releasedByGesture: false,
      }),
    ).toBeNull();
  });
});

describe("feedResizeScrollFollowDecision", () => {
  const client = 400;
  const offset = 400;

  it("no-ops on occluded 0×0 scroll parent", () => {
    expect(
      feedResizeScrollFollowDecision({
        scrollHeight: 2000,
        scrollTop: 0,
        clientHeight: 0,
        offsetHeight: 0,
        snapshotPinned: true,
        snapshotSettling: false,
        snapshotScrollTop: 0,
        snapshotScrollHeight: 2000,
        releasedByGesture: false,
      }),
    ).toEqual({ kind: "noop" });
  });

  it("follows large growth from pinned snapshot despite post-resize unpinned geometry", () => {
    const oldMax = 1600 - client;
    const newHeight = 2200;
    expect(
      feedResizeScrollFollowDecision({
        scrollHeight: newHeight,
        scrollTop: oldMax,
        clientHeight: client,
        offsetHeight: offset,
        snapshotPinned: true,
        snapshotSettling: false,
        snapshotScrollTop: oldMax,
        snapshotScrollHeight: 1600,
        releasedByGesture: false,
      }),
    ).toEqual({ kind: "follow", scrollTop: newHeight - client });
  });

  it("cancels when gesture release lands between observation and rAF", () => {
    expect(
      feedResizeScrollFollowDecision({
        scrollHeight: 2200,
        scrollTop: 400,
        clientHeight: client,
        offsetHeight: offset,
        snapshotPinned: true,
        snapshotSettling: true,
        snapshotScrollTop: 1600,
        snapshotScrollHeight: 2200,
        releasedByGesture: true,
      }),
    ).toEqual({ kind: "noop" });
  });

  it("no-ops when keyboard/scrollbar scroll-up occurs between observation and rAF with stable height", () => {
    const oldMax = 1600 - client;
    expect(
      feedResizeScrollFollowDecision({
        scrollHeight: 1600,
        scrollTop: 400,
        clientHeight: client,
        offsetHeight: offset,
        snapshotPinned: true,
        snapshotSettling: false,
        snapshotScrollTop: oldMax,
        snapshotScrollHeight: 1600,
        releasedByGesture: false,
      }),
    ).toEqual({ kind: "noop" });
  });

  it("no-ops when scrollTop drops between observation and rAF while content grew", () => {
    const oldMax = 1600 - client;
    const newHeight = 2200;
    expect(
      feedResizeScrollFollowDecision({
        scrollHeight: newHeight,
        scrollTop: 400,
        clientHeight: client,
        offsetHeight: offset,
        snapshotPinned: true,
        snapshotSettling: false,
        snapshotScrollTop: oldMax,
        snapshotScrollHeight: 1600,
        releasedByGesture: false,
      }),
    ).toEqual({ kind: "noop" });
  });

  it("follows pinned collapse when content shrink clamps scrollTop and scrollHeight", () => {
    const oldMax = 1600 - client;
    const shrunkHeight = 1200;
    const clampedTop = shrunkHeight - client;
    expect(
      feedResizeScrollFollowDecision({
        scrollHeight: shrunkHeight,
        scrollTop: clampedTop,
        clientHeight: client,
        offsetHeight: offset,
        snapshotPinned: true,
        snapshotSettling: false,
        snapshotScrollTop: oldMax,
        snapshotScrollHeight: 1600,
        releasedByGesture: false,
      }),
    ).toEqual({ kind: "restore_pin_only" });
  });

  it("restores pin ownership when already at max with valid pinned snapshot", () => {
    const max = 2000 - client;
    expect(
      feedResizeScrollFollowDecision({
        scrollHeight: 2000,
        scrollTop: max,
        clientHeight: client,
        offsetHeight: offset,
        snapshotPinned: true,
        snapshotSettling: false,
        snapshotScrollTop: max,
        snapshotScrollHeight: 2000,
        releasedByGesture: false,
      }),
    ).toEqual({ kind: "restore_pin_only" });
  });

  it("no-ops when snapshot was unpinned and not settling", () => {
    expect(
      feedResizeScrollFollowDecision({
        scrollHeight: 2200,
        scrollTop: 400,
        clientHeight: client,
        offsetHeight: offset,
        snapshotPinned: false,
        snapshotSettling: false,
        snapshotScrollTop: 400,
        snapshotScrollHeight: 2200,
        releasedByGesture: false,
      }),
    ).toEqual({ kind: "noop" });
  });
});

describe("mergeFeedResizeObservationSnapshots", () => {
  it("merges pinned/settling monotonically while preserving earliest scroll geometry", () => {
    const first = {
      pinned: false,
      settling: false,
      scrollTop: 400,
      scrollHeight: 2000,
    };
    const second = {
      pinned: true,
      settling: true,
      scrollTop: 1600,
      scrollHeight: 2200,
    };
    expect(mergeFeedResizeObservationSnapshots(first, second)).toEqual({
      pinned: true,
      settling: true,
      scrollTop: 400,
      scrollHeight: 2000,
    });
  });

  it("does not downgrade pinned ownership from a later unpinned callback", () => {
    const first = {
      pinned: true,
      settling: false,
      scrollTop: 1600,
      scrollHeight: 2000,
    };
    const second = {
      pinned: false,
      settling: false,
      scrollTop: 400,
      scrollHeight: 2000,
    };
    expect(mergeFeedResizeObservationSnapshots(first, second)).toEqual({
      pinned: true,
      settling: false,
      scrollTop: 1600,
      scrollHeight: 2000,
    });
  });
});

describe("shouldCancelFeedResizeFollowForManualScrollAway", () => {
  it("detects manual scroll-away when scrollTop drops with stable height", () => {
    expect(
      shouldCancelFeedResizeFollowForManualScrollAway({
        snapshotScrollTop: 1200,
        snapshotScrollHeight: 2000,
        liveScrollTop: 400,
        liveScrollHeight: 2000,
      }),
    ).toBe(true);
  });

  it("does not treat content shrink clamp as manual scroll-away", () => {
    expect(
      shouldCancelFeedResizeFollowForManualScrollAway({
        snapshotScrollTop: 1200,
        snapshotScrollHeight: 1600,
        liveScrollTop: 800,
        liveScrollHeight: 1200,
      }),
    ).toBe(false);
  });
});

describe("chooseFeedFollowFlush", () => {
  it("applies stick-to-bottom before paint (not rAF)", () => {
    expect(chooseFeedFollowFlush()).toBe("before_paint");
  });
});

describe("scrollTopAfterFeedHeightChange chrome shrink", () => {
  it("pins to the new max when composer/status chrome shrinks the viewport", () => {
    const height = 2000;
    const oldClient = 400;
    const newClient = 320;
    expect(
      scrollTopAfterFeedHeightChange({
        scrollHeight: height,
        scrollTop: height - oldClient,
        clientHeight: newClient,
        pinned: true,
        settling: false,
        releasedByGesture: false,
      }),
    ).toBe(height - newClient);
  });

  it("does not fight an unpinned reader when chrome grows", () => {
    const height = 2000;
    expect(
      scrollTopAfterFeedHeightChange({
        scrollHeight: height,
        scrollTop: 200,
        clientHeight: 320,
        pinned: false,
        settling: false,
        releasedByGesture: true,
      }),
    ).toBeNull();
  });
});

describe("feedBottomClearancePx", () => {
  it("matches measured chrome and ignores junk heights", () => {
    expect(feedBottomClearancePx(180)).toBe(180);
    expect(feedBottomClearancePx(0)).toBe(96);
    expect(feedBottomClearancePx(12)).toBe(72);
    expect(feedBottomClearancePx(800)).toBe(480);
  });
});

describe("feedScroll user-gesture deferral", () => {
  const height = 2000;
  const client = 400;
  const max = height - client;

  it("defers follow while a user gesture is active", () => {
    expect(shouldDeferFollowDuringUserGesture(true)).toBe(true);
    expect(shouldDeferFollowDuringUserGesture(false)).toBe(false);
    expect(FEED_GESTURE_IDLE_MS).toBe(150);
  });

  it("does not re-pin into the 28px band while the user gesture is still active", () => {
    const lightUpTop = height - client - 40;
    expect(
      nextFeedPinState({
        wasPinned: false,
        releasedByGesture: true,
        scrollHeight: height,
        scrollTop: height - client - 10,
        clientHeight: client,
        prevScrollTop: lightUpTop,
        settling: false,
        repinPx: FEED_REPIN_THRESHOLD_PX,
        userGestureActive: true,
      }),
    ).toEqual({ pinned: false, releasedByGesture: true });
    expect(
      scrollTopAfterFeedHeightChange({
        scrollHeight: height + 80,
        scrollTop: height - client - 10,
        clientHeight: client,
        pinned: false,
        settling: false,
        releasedByGesture: true,
        userGestureActive: true,
      }),
    ).toBeNull();
    expect(
      feedResizeScrollFollowDecision({
        scrollHeight: height + 80,
        scrollTop: height - client - 10,
        clientHeight: client,
        offsetHeight: client,
        snapshotPinned: false,
        snapshotSettling: false,
        snapshotScrollTop: height - client - 10,
        snapshotScrollHeight: height,
        releasedByGesture: true,
        userGestureActive: true,
      }),
    ).toEqual({ kind: "noop" });
  });

  it("does not steal scrollTop mid-flick above the tail", () => {
    expect(
      scrollTopAfterFeedHeightChange({
        scrollHeight: 2400,
        scrollTop: 800,
        clientHeight: client,
        pinned: false,
        settling: false,
        releasedByGesture: true,
        userGestureActive: true,
      }),
    ).toBeNull();
    expect(
      feedResizeScrollFollowDecision({
        scrollHeight: 2400,
        scrollTop: 800,
        clientHeight: client,
        offsetHeight: client,
        snapshotPinned: true,
        snapshotSettling: false,
        snapshotScrollTop: max,
        snapshotScrollHeight: height,
        releasedByGesture: true,
        userGestureActive: true,
      }),
    ).toEqual({ kind: "noop" });
  });

  it("re-pins after gesture idle when near the tight bottom band", () => {
    const lightUpTop = height - client - 40;
    expect(
      nextFeedPinState({
        wasPinned: false,
        releasedByGesture: true,
        scrollHeight: height,
        scrollTop: height - client - 10,
        clientHeight: client,
        prevScrollTop: lightUpTop,
        settling: false,
        repinPx: FEED_REPIN_THRESHOLD_PX,
        userGestureActive: false,
      }),
    ).toEqual({ pinned: true, releasedByGesture: false });
    expect(
      scrollTopAfterFeedHeightChange({
        scrollHeight: height,
        scrollTop: height - client - 10,
        clientHeight: client,
        pinned: true,
        settling: false,
        releasedByGesture: false,
        userGestureActive: false,
      }),
    ).toBe(max);
  });

  it("keeps pin when growth leaves the old max outside the re-pin band", () => {
    const oldMax = 1600 - client;
    const newHeight = 2200;
    expect(newHeight - oldMax - client).toBeGreaterThan(FEED_REPIN_THRESHOLD_PX);
    expect(
      nextFeedPinState({
        wasPinned: true,
        releasedByGesture: false,
        scrollHeight: newHeight,
        scrollTop: oldMax,
        clientHeight: client,
        prevScrollTop: oldMax,
        settling: false,
        userGestureActive: false,
      }),
    ).toEqual({ pinned: true, releasedByGesture: false });
  });

  it("follows to the new max after arriving at the tail plus immediate growth", () => {
    const arrived = nextFeedPinState({
      wasPinned: false,
      releasedByGesture: true,
      scrollHeight: height,
      scrollTop: max,
      clientHeight: client,
      prevScrollTop: max - 20,
      settling: false,
      userGestureActive: true,
    });
    expect(arrived).toEqual({ pinned: true, releasedByGesture: false });
    const newHeight = height + 120;
    expect(
      nextFeedPinState({
        wasPinned: arrived.pinned,
        releasedByGesture: arrived.releasedByGesture,
        scrollHeight: newHeight,
        scrollTop: max,
        clientHeight: client,
        prevScrollTop: max,
        settling: false,
        userGestureActive: true,
      }),
    ).toEqual({ pinned: true, releasedByGesture: false });
    expect(
      scrollTopAfterFeedHeightChange({
        scrollHeight: newHeight,
        scrollTop: max,
        clientHeight: client,
        pinned: true,
        settling: false,
        releasedByGesture: false,
        userGestureActive: true,
      }),
    ).toBe(newHeight - client);
    expect(
      feedResizeScrollFollowDecision({
        scrollHeight: newHeight,
        scrollTop: max,
        clientHeight: client,
        offsetHeight: client,
        snapshotPinned: true,
        snapshotSettling: false,
        snapshotScrollTop: max,
        snapshotScrollHeight: height,
        releasedByGesture: false,
        userGestureActive: true,
      }),
    ).toEqual({ kind: "follow", scrollTop: newHeight - client });
  });
});
