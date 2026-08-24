/**
 * Feed Motion leftovers (v0.9.331). Live tail + fallback are plain divs —
 * no motion.div layout and no popLayout presence wrapper. Virtual window rows
 * never take a Motion layout transform — TanStack owns translateY.
 * ConversationChatColumn still uses layoutScroll on the Pretext / overflow-anchor
 * scrollport. prefers-reduced-motion stays off (animations run unless opted out).
 */

import { useReducedMotion } from "motion/react";

/** Whether feed layout/presence animations should run. */
export function feedLayoutMotionEnabled(reducedMotion: boolean | null): boolean {
  return !reducedMotion;
}

/** Whether feed layout/presence animations should run. */
export function useFeedLayoutMotion(): boolean {
  return feedLayoutMotionEnabled(useReducedMotion());
}

/**
 * Virtual window rows are `absolute top-0` + virtualizer translateY.
 * Motion `layout` / popLayout write `transform` and stack every row at y=0.
 * Kept false: do not re-enable layout on virtual rows.
 */
export const VIRTUAL_ROW_LAYOUT_ENABLED = false;
