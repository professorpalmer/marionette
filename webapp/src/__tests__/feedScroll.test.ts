import { describe, expect, it } from "vitest";
import {
  FEED_REPIN_THRESHOLD_PX,
  nextFeedPinState,
  settleFrameResult,
  shouldShowJumpToBottom,
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

  it("settle glue must not force-true after a gesture release", () => {
    expect(shouldUnpinOnWheel(-12, false)).toBe(true);
    expect(shouldUnpinOnWheel(-12, true)).toBe(false);
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
