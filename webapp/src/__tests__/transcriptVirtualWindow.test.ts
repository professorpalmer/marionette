import { describe, expect, it } from "vitest";
import {
  isOccludedScrollParentSize,
  restoreFeedScrollAfterFocus,
  shouldUseVirtualTranscriptWindow,
} from "../components/conversation/transcriptVirtualWindow";

describe("transcriptVirtualWindow", () => {
  it("treats a 0x0 scroll parent as occluded, not unsized-forever", () => {
    expect(isOccludedScrollParentSize(0, 0)).toBe(true);
    expect(isOccludedScrollParentSize(360, 0)).toBe(false);
    expect(isOccludedScrollParentSize(0, 360)).toBe(false);
  });

  it("latches virtualization after the feed has been sized once", () => {
    expect(
      shouldUseVirtualTranscriptWindow({
        scrollParentSized: false,
        alreadyVirtualized: false,
      }),
    ).toBe(false);
    expect(
      shouldUseVirtualTranscriptWindow({
        scrollParentSized: true,
        alreadyVirtualized: false,
      }),
    ).toBe(true);
    expect(
      shouldUseVirtualTranscriptWindow({
        scrollParentSized: false,
        alreadyVirtualized: true,
      }),
    ).toBe(true);
  });

  it("restores saved offset when unpinned, bottom when pinned", () => {
    expect(
      restoreFeedScrollAfterFocus({
        savedScrollTop: 1400,
        pinned: false,
        settling: false,
        scrollHeight: 8000,
      }),
    ).toBe(1400);
    expect(
      restoreFeedScrollAfterFocus({
        savedScrollTop: 1400,
        pinned: true,
        settling: false,
        scrollHeight: 8000,
      }),
    ).toBe(8000);
    expect(
      restoreFeedScrollAfterFocus({
        savedScrollTop: 1400,
        pinned: false,
        settling: true,
        scrollHeight: 8000,
      }),
    ).toBe(8000);
  });
});
