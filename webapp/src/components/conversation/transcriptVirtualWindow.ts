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

/**
 * Keep the chat column mounted while a file tab is selected.
 * Unmounting remounts the virtualizer at offset 0 — the same snap-to-top
 * as an occluded unvirtualized fallback. Hide in-place instead.
 */
export function chatColumnMountClass(activeTab: string): string {
  const base = "flex flex-col min-h-0 min-w-0";
  if (activeTab === "chat") return `${base} flex-1`;
  return `${base} pointer-events-none invisible absolute inset-0`;
}

export function isChatColumnActive(activeTab: string): boolean {
  return activeTab === "chat";
}
