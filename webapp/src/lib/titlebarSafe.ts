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
