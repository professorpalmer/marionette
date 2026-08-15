/**
 * Virtual transcript window vs full-list fallback.
 *
 * Chromium/Electron often reports clientHeight=0 while the window is
 * blurred or occluded (alt-tab). Falling back to the unvirtualized list
 * remounts every bubble and snaps the feed to the top. Latch virtualization
 * once the scroll parent has been sized, and ignore zero-size resizes.
 */

export function isOccludedScrollParentSize(
  clientHeight: number,
  offsetHeight: number,
): boolean {
  return clientHeight <= 0 && offsetHeight <= 0;
}

export function shouldUseVirtualTranscriptWindow(opts: {
  scrollParentSized: boolean;
  alreadyVirtualized: boolean;
}): boolean {
  return opts.alreadyVirtualized || opts.scrollParentSized;
}

/** Scroll offset to apply after the window is focused again. */
export function restoreFeedScrollAfterFocus(opts: {
  savedScrollTop: number;
  pinned: boolean;
  settling: boolean;
  scrollHeight: number;
}): number {
  if (opts.pinned || opts.settling) return Math.max(0, opts.scrollHeight);
  return Math.max(0, opts.savedScrollTop);
}
