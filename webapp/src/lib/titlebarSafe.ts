/** Fixed macOS hiddenInset traffic-light / chrome clearances (px, not rem).

Rem-based Tailwind padding (`pl-24`, `px-6`) shrinks when `html { font-size: clamp(...) }`
responds to window resize — which obscured top strips under the traffic lights.
These constants are applied as inline `px` styles so root font changes cannot
reduce clearance.
*/
/** Right edge of the macOS hiddenInset traffic lights (they sit at x ~12-70).
Any content row that can be the topmost row must start past this. */
export const MACOS_TRAFFIC_LIGHT_RIGHT_EDGE_PX = 70;

export const TITLEBAR_TRAFFIC_PAD_PX = 120;
export const TITLEBAR_TRAFFIC_PAD_SM_PX = 112;
export const TITLEBAR_CHROME_PAD_X_PX = 24;

/** Rows whose top edge is within this band can sit beside the traffic lights. */
export const MACOS_TITLEBAR_BAND_PX = 40;

/**
 * Left padding for a top strip at `rect` (window coordinates): just enough for
 * its content to start past the traffic lights when it actually sits beside
 * them, and the ordinary chrome padding anywhere else (below the title bar, or
 * right of an open left rail). A fixed traffic pad on a strip that starts at
 * x=190 was a visible gap before the brand.
 */
export function titlebarLeftPad(rect: { left: number; top: number }): number {
  if (rect.top >= MACOS_TITLEBAR_BAND_PX) return TITLEBAR_CHROME_PAD_X_PX;
  return Math.max(TITLEBAR_CHROME_PAD_X_PX, TITLEBAR_TRAFFIC_PAD_PX - Math.max(0, rect.left));
}
